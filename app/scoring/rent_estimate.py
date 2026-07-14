"""
Automated commercial-rent estimation — the legal, free, no-new-API path.

There is no free commercial-rent API, and the listing sites (LoopNet, Crexi,
CoStar) prohibit scraping. So we don't fetch rent — we *estimate* it from
signals already collected, and label the result ``estimated`` so it is never
confused with a cited quote.

Two outputs:

1. ``rent_pressure_index`` (0..100) — a relative "how expensive is this
   corridor likely to be" score, derived from tract median income, nearby
   commercial/POI density, competitor density, and business-corridor
   proximity. Always available; pure function of collected signals.

2. ``estimated_rent_per_sqft`` ($/sqft/yr) — only produced when at least one
   *cited* rent anchor is supplied (via ``data/raw/rent_overrides.csv``).
   The index is linearly calibrated to the anchor points, so a single real
   number per metro lets us extrapolate dollar estimates to every candidate.
   Without an anchor this stays ``None`` — we never invent a dollar figure.

The cited-override path (``rent_score`` / ``rent_overrides.csv``) remains the
authoritative source; this module only fills the gap where no override
matched, and always flags itself ``estimated``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.config import RENT_PRESSURE_BOUNDS


@dataclass
class RentEstimate:
    rent_pressure_index: float           # 0..100, always present
    estimated_rent_per_sqft: Optional[float]   # $/sqft/yr, only if anchored
    confidence: str                      # "estimated" | "estimated_anchored"
    rationale: List[str]


def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _norm(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None or high <= low:
        return None
    return max(0.0, min(1.0, (value - low) / (high - low)))


def compute_rent_pressure_index(profile: Dict[str, object]) -> Tuple[float, List[str]]:
    """Relative 0..100 estimate of commercial-rent pressure for a candidate.

    Higher = pricier corridor. Pure function of already-collected signals:
      - tract median household income (proxy for area affluence / rent)
      - nearby commercial/POI + competitor density (demand for space)
      - business-corridor / airport proximity (prime-location premium)
    Components with missing data are dropped and the weights renormalized,
    so a thin-data candidate still gets a best-effort index rather than 0.
    """
    bullets: List[str] = []
    components: List[Tuple[float, float]] = []  # (normalized 0..1, weight)

    # 1) Tract median household income — the strongest single rent proxy.
    census = (profile.get("economy") or {}).get("census") or {}
    income = _num((census.get("values") or {}).get("median_household_income"))
    income_n = _norm(income, *RENT_PRESSURE_BOUNDS["income"])
    if income_n is not None:
        components.append((income_n, 0.40))
        bullets.append(f"tract median income ${income:,.0f}")

    # 2) Commercial / demand density nearby — competition for storefront space.
    # Only counts when demand data was actually collected (non-empty dict);
    # an empty counts block is "missing", not "zero density".
    counts_5mi = profile.get("counts_5mi") or {}
    if isinstance(counts_5mi, dict) and counts_5mi:
        total_nearby = sum(int(v or 0) for v in counts_5mi.values())
        density_n = _norm(float(total_nearby), *RENT_PRESSURE_BOUNDS["density"])
        if density_n is not None:
            components.append((density_n, 0.25))
            bullets.append(f"{total_nearby} nearby demand/POI signals")

    # 3) Competitor density — saturated corridors command higher rent.
    comp = profile.get("competition_summary") or {}
    comp_total = _num(comp.get("competitor_count_total"))
    comp_n = _norm(comp_total, *RENT_PRESSURE_BOUNDS["competition"])
    if comp_n is not None:
        components.append((comp_n, 0.20))
        bullets.append(f"{int(comp_total)} CPR/BLS competitors nearby")

    # 4) Business-corridor / airport proximity — prime-location premium.
    signals = (profile.get("accessibility") or {}).get("signals") or {}
    corridor = signals.get("airport_business_corridor_proximity") or {}
    if isinstance(corridor, dict) and corridor.get("status") == "detected":
        dist = _num(corridor.get("distance_miles"))
        if dist is not None:
            # closer = higher pressure; 0mi→1.0, max_mi→0.0 (inverted range, so
            # we compute directly rather than via _norm which needs low<high).
            max_mi = RENT_PRESSURE_BOUNDS["corridor_miles"][1]
            prox_n = (max(0.0, min(1.0, (max_mi - dist) / max_mi))
                      if max_mi > 0 else 0.0)
            components.append((prox_n, 0.15))
            bullets.append(f"business corridor {dist:.1f} mi away")

    if not components:
        return 50.0, ["no usable signals — neutral rent-pressure estimate"]

    weight_total = sum(w for _, w in components)
    index = sum(v * w for v, w in components) / weight_total * 100.0
    return round(index, 2), bullets


def estimate_rent_per_sqft(
    rent_pressure_index: float,
    anchors: List[Tuple[float, float]],
) -> Optional[float]:
    """Calibrate the pressure index into $/sqft using cited anchor points.

    ``anchors`` is a list of ``(rent_pressure_index, cited_rent_per_sqft)``
    pairs from other candidates in the same run that DID match a cited
    override. With 1 anchor we scale proportionally; with 2+ we fit a simple
    linear map (index → $/sqft). Returns ``None`` when no anchors exist —
    we never invent dollars without a cited reference.
    """
    pts = [(i, r) for i, r in anchors
           if isinstance(i, (int, float)) and isinstance(r, (int, float))]
    if not pts:
        return None
    if len(pts) == 1:
        idx0, rent0 = pts[0]
        if idx0 <= 0:
            return round(rent0, 2)
        return round(rent0 * (rent_pressure_index / idx0), 2)

    # Least-squares linear fit rent = a*index + b.
    n = len(pts)
    sx = sum(i for i, _ in pts)
    sy = sum(r for _, r in pts)
    sxx = sum(i * i for i, _ in pts)
    sxy = sum(i * r for i, r in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return round(sy / n, 2)
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return round(max(0.0, a * rent_pressure_index + b), 2)


def build_rent_estimate(
    profile: Dict[str, object],
    anchors: Optional[List[Tuple[float, float]]] = None,
) -> RentEstimate:
    index, bullets = compute_rent_pressure_index(profile)
    dollars = estimate_rent_per_sqft(index, anchors or [])
    if dollars is not None:
        confidence = "estimated_anchored"
        bullets.append(
            f"calibrated to {len(anchors)} cited rent anchor(s) → "
            f"~${dollars:,.0f}/sqft/yr (estimated)"
        )
    else:
        confidence = "estimated"
        bullets.append(
            "no cited rent anchor in this run — relative index only "
            "(supply one rent_overrides.csv point to get $/sqft estimates)"
        )
    return RentEstimate(
        rent_pressure_index=index,
        estimated_rent_per_sqft=dollars,
        confidence=confidence,
        rationale=bullets,
    )


def _cited_rent(profile: Dict[str, object]) -> Optional[float]:
    """Return a candidate's cited $/sqft from a matched override, else None."""
    real_estate = (profile.get("economy") or {}).get("real_estate") or {}
    values = real_estate.get("values") or {}
    rent = values.get("rent_per_sqft_annual")
    return float(rent) if isinstance(rent, (int, float)) else None


def apply_rent_estimates(ranked) -> None:
    """Cohort post-pass: write a rent estimate onto every candidate's scored dict.

    1. Compute each candidate's rent_pressure_index.
    2. Collect (index, cited_rent) anchors from candidates whose run matched
       a cited override.
    3. Calibrate index → $/sqft for every candidate using those anchors
       (or leave dollars None when the cohort has no cited rent at all).

    Stored under ``scored["rent_estimate"]``. Never overrides the
    authoritative cited rent in ``scored["rent"]``.
    """
    indices: List[Tuple[object, float]] = []
    anchors: List[Tuple[float, float]] = []
    for profile, scored in ranked:
        index, _ = compute_rent_pressure_index(profile)
        indices.append((scored, index))
        cited = _cited_rent(profile)
        if cited is not None:
            anchors.append((index, cited))

    for (profile, scored), (scored_ref, index) in zip(ranked, indices):
        dollars = estimate_rent_per_sqft(index, anchors)
        confidence = "estimated_anchored" if dollars is not None else "estimated"
        scored["rent_estimate"] = {
            "rent_pressure_index": index,
            "estimated_rent_per_sqft": dollars,
            "confidence": confidence,
            "anchor_count": len(anchors),
        }
