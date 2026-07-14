"""
Confidence score — 0..100.

Reflects how much we trust the inputs feeding the site score. It is NOT a
measure of the recommendation's correctness; it's a measure of data quality.

Inputs feeding confidence:
  - source count: how many distinct, citable sources backed this profile.
  - source quality: official sources (Census, BLS) weighted higher than
    Google Places.
  - missing fields: each tracked-but-unfilled field deducts.
  - freshness: collected_at within last 30 days is full credit; >12 months
    is heavily discounted. (For ACS, "data_year" is annual by design — we
    treat the ACS row as fresh if we just fetched it.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


# Source name -> trust weight (higher = more credible).
SOURCE_TRUST = {
    "us census bureau acs": 1.0,
    "bls": 1.0,
    "google places api": 0.8,
    "manual rent overrides": 0.9,
    "public job postings csv": 0.9,
}


@dataclass
class ConfidenceBreakdown:
    score: float
    rationale: List[str]
    dimensions: Dict[str, float] = None  # per-axis 0..100 confidence

    def __post_init__(self) -> None:
        if self.dimensions is None:
            self.dimensions = {}


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _freshness(ts: str) -> float:
    """1.0 if collected <30 days ago, sliding to 0.4 at 12 months, 0.2 beyond."""
    parsed = _parse_iso(ts)
    if parsed is None:
        return 0.5
    days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    if days < 0:
        return 1.0
    if days <= 30:
        return 1.0
    if days <= 365:
        return 1.0 - 0.6 * ((days - 30) / 335)
    return 0.2


def _trust(source_name: str) -> float:
    """Return the trust weight for a source name (0.0 if unrecognized).

    Unrecognized sources return 0.0 deliberately: a source the scorer has no
    explicit rating for is treated like a stub and excluded from the quality
    average rather than asserting a fabricated "neutral" trust. Add the source
    to ``SOURCE_TRUST`` to grant it credibility.
    """
    name = (source_name or "").lower()
    if "stub" in name or "not yet integrated" in name:
        return 0.0
    best = 0.0
    for key, val in SOURCE_TRUST.items():
        if key in name:
            best = max(best, val)
    return best


def _dimension_demographic(profile: Dict[str, object]) -> float:
    census = ((profile.get("economy") or {}).get("census") or {})  # type: ignore[union-attr]
    if not isinstance(census, dict):
        return 0.0
    confidence = str(census.get("data_confidence") or "unknown").lower()
    if confidence == "full":
        return 1.0
    if confidence == "partial":
        return 0.6
    if confidence == "missing":
        return 0.0
    values = census.get("values") or {}
    filled = sum(1 for v in values.values() if v is not None)
    total = max(len(values), 1)
    return filled / total


def _dimension_accessibility(profile: Dict[str, object]) -> float:
    accessibility = profile.get("accessibility") or {}
    if not isinstance(accessibility, dict):
        return 0.0
    signals = accessibility.get("signals") or {}
    if not isinstance(signals, dict) or not signals:
        return 0.0
    known = 0
    total = 0
    for sig in signals.values():
        if not isinstance(sig, dict):
            continue
        total += 1
        status = str(sig.get("status") or "unknown").lower()
        if status in ("detected", "not_detected", "missing"):
            known += 1
    return known / total if total else 0.0


def _dimension_rent(profile: Dict[str, object]) -> float:
    economy = profile.get("economy") or {}
    if not isinstance(economy, dict):
        return 0.0
    real_estate = economy.get("real_estate") or {}
    if not isinstance(real_estate, dict):
        return 0.0
    confidence = str(real_estate.get("data_confidence") or "unknown").lower()
    if confidence in ("manual_override", "override", "verified"):
        return 1.0
    if confidence in ("partial",):
        return 0.5
    return 0.0


def _dimension_competition(profile: Dict[str, object]) -> float:
    summary = profile.get("competition_summary") or {}
    if not isinstance(summary, dict):
        return 0.0
    total = int(summary.get("competitor_count_total") or 0)
    if total <= 0:
        # No competitors found is itself a finding; treat as moderate.
        return 0.5
    checked = int(summary.get("website_analysis_checked_count") or 0)
    return min(1.0, checked / max(total, 1))


def _dimension_demand(profile: Dict[str, object]) -> float:
    counts_5mi = profile.get("counts_5mi") or {}
    if not isinstance(counts_5mi, dict) or not counts_5mi:
        return 0.0
    populated = sum(1 for v in counts_5mi.values() if int(v or 0) > 0)
    total = max(len(counts_5mi), 1)
    return min(1.0, populated / total * 1.5)


def _dimension_freshness(profile: Dict[str, object]) -> float:
    sources: List[Dict[str, object]] = profile.get("sources") or []  # type: ignore[assignment]
    fresh_terms: List[float] = []
    for s in sources:
        if _trust(str(s.get("name", ""))) <= 0:
            continue
        fresh_terms.append(_freshness(str(s.get("collected_at", ""))))
    return sum(fresh_terms) / len(fresh_terms) if fresh_terms else 0.0


def _dimension_saturation(profile: Dict[str, object]) -> float:
    """Per-candidate. Drops when many demand categories hit the ≥20 Google
    Places page-limit cap — we don't actually know whether the area has
    21 or 80 of that category, so the underlying count is fuzzy.

    Reads ``profile["saturated_demand_categories"]`` populated by the demand
    enricher. Score is ``1 - (saturated / non_zero_categories)``.
    """
    saturated = profile.get("saturated_demand_categories") or []
    counts_5mi = profile.get("counts_5mi") or {}
    if not isinstance(counts_5mi, dict) or not counts_5mi:
        return 0.0
    non_zero = sum(1 for v in counts_5mi.values() if int(v or 0) > 0)
    if non_zero <= 0:
        return 0.0
    share_saturated = min(1.0, len(saturated) / non_zero)
    return max(0.0, 1.0 - share_saturated)


def compute_confidence_score(profile: Dict[str, object]) -> ConfidenceBreakdown:
    sources: List[Dict[str, object]] = profile.get("sources") or []  # type: ignore[assignment]
    missing: List[str] = profile.get("missing_fields") or []  # type: ignore[assignment]

    # Source quality: average trust * freshness across non-stub sources.
    quality_terms: List[float] = []
    for s in sources:
        name = str(s.get("name", ""))
        ts = str(s.get("collected_at", ""))
        trust = _trust(name)
        if trust <= 0:
            continue  # stub source contributes nothing
        quality_terms.append(trust * _freshness(ts))

    quality = sum(quality_terms) / len(quality_terms) if quality_terms else 0.0

    # Source breadth: log-style saturation, capped at 4 unique credible sources.
    distinct_credible = len({
        s.get("name", "") for s in sources if _trust(str(s.get("name", ""))) > 0
    })
    breadth = min(1.0, distinct_credible / 4.0)

    # Missing-field penalty.
    penalty = min(0.5, 0.04 * len(missing))

    score_01 = max(0.0, (0.6 * quality + 0.4 * breadth) - penalty)

    dimensions_01: Dict[str, float] = {
        "demographic": _dimension_demographic(profile),
        "accessibility": _dimension_accessibility(profile),
        "rent": _dimension_rent(profile),
        "competition": _dimension_competition(profile),
        "demand": _dimension_demand(profile),
        "data_freshness": _dimension_freshness(profile),
        "saturation": _dimension_saturation(profile),
        # catchment_overlap + differentiation are cohort-level; populated
        # in cohort_normalization.apply_cohort_confidence after all
        # candidates are scored. Placeholder until then so the renderer
        # doesn't show "missing" when cohort=1.
        "catchment_overlap": 1.0,
        "differentiation": 1.0,
    }
    dimensions = {k: round(v * 100, 2) for k, v in dimensions_01.items()}

    bullets: List[str] = [
        f"{distinct_credible} distinct credible source(s)",
        f"source quality factor {quality:.2f}",
    ]
    if missing:
        bullets.append(f"{len(missing)} missing field(s): "
                       f"{', '.join(missing[:5])}"
                       f"{'…' if len(missing) > 5 else ''}")
    weakest = sorted(dimensions.items(), key=lambda kv: kv[1])[:2]
    if weakest:
        bullets.append(
            "weakest dimensions: "
            + "; ".join(f"{k} {v:.0f}/100" for k, v in weakest)
        )
    return ConfidenceBreakdown(
        score=round(score_01 * 100, 2),
        rationale=bullets,
        dimensions=dimensions,
    )
