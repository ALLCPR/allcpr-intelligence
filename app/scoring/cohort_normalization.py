"""
Cohort z-score normalization.

The absolute scoring curves in ``demand_score`` / ``competition_score`` use a
saturating ``min(1, count / cap)`` shape — fine in suburban markets, but in a
dense urban metro every candidate hits the cap on most categories and the
final ``site_score`` lands in a tiny band (SF: 71.5–75.7 across 9 candidates).

This module runs as a second pass after every candidate has been scored. It
computes per-cohort means and standard deviations of the underlying *counts*
(demand drivers, competitor density) plus the relevant sub-scores, then
re-projects each candidate using a sigmoid-mapped z-score blended with the
original absolute score. The output:

- preserves absolute meaning: a near-empty market doesn't suddenly look
  competitive just because the cohort is empty,
- but exposes intra-cohort differentiation: 13 hospitals vs 6 hospitals
  in the same metro no longer reads as "both 100, identical."

A cohort of one candidate is skipped (nothing to normalize against).
"""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

from app.config import COHORT_BLEND, SCORE_WEIGHTS

# COHORT_BLEND is re-exported from app.config (single source of truth + env
# override). Kept importable from this module for backward compatibility.
__all__ = ["COHORT_BLEND", "apply_cohort_normalization", "apply_cohort_confidence",
           "cohort_means_from_ranked", "factor_decomposition"]

# Sub-scores that benefit from cohort normalization. These are the ones that
# saturate hardest in dense markets.
NORMALIZABLE_SUB_SCORES: Tuple[str, ...] = (
    "demand_score",
    "healthcare_training_ecosystem_score",
    "competition_gap_score",
    "allcpr_opportunity_score",
)


def _sigmoid(z: float) -> float:
    """Smoothly map a z-score into 0..1 space.

    z=0 → 0.5, z=+1 → ~0.73, z=+2 → ~0.88, z=-2 → ~0.12. Caps off cleanly
    so a wild outlier in the cohort doesn't pin everyone else to 0.
    """
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0


def _cohort_stats(values: List[float]) -> Tuple[float, float]:
    """Return (mean, stdev). A degenerate cohort (no spread) returns sigma=0.0
    as a sentinel — callers must skip normalization for that sub-score rather
    than fabricating a unit sigma, which would drag a uniform cohort toward 50.
    """
    if not values:
        return 0.0, 0.0
    mu = mean(values)
    sigma = pstdev(values) if len(values) > 1 else 0.0
    if sigma < 1e-6:
        return mu, 0.0  # degenerate cohort — no spread; signal "do not normalize"
    return mu, sigma


def _blend(absolute: float, relative: float, blend: float = COHORT_BLEND) -> float:
    """Convex combination clipped to [0, 100]."""
    out = (1.0 - blend) * absolute + blend * relative
    return max(0.0, min(100.0, out))


def apply_cohort_normalization(
    ranked: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    blend: float = COHORT_BLEND,
) -> List[Dict[str, Any]]:
    """Recompute saturated sub-scores using cohort-relative z-scores.

    Mutates ``scored`` dicts in place: replaces affected sub-scores with the
    blended values, records the originals under ``scored["cohort_normalization"]``
    for the report, and recomputes ``site_score`` from the new sub_scores.
    Returns a list of cohort summary rows for the source audit.

    Cohorts smaller than 2 candidates are skipped — there's nothing to
    normalize against.
    """
    if len(ranked) < 2:
        return []

    # Collect each candidate's absolute sub-score values.
    by_key: Dict[str, List[float]] = {k: [] for k in NORMALIZABLE_SUB_SCORES}
    for _, scored in ranked:
        sub = scored.get("sub_scores") or {}
        for key in NORMALIZABLE_SUB_SCORES:
            v = sub.get(key)
            if isinstance(v, (int, float)):
                by_key[key].append(float(v))

    cohort_stats = {k: _cohort_stats(by_key[k]) for k in NORMALIZABLE_SUB_SCORES}

    # Track originals + decomposition for explainability.
    summary_rows: List[Dict[str, Any]] = []
    for profile, scored in ranked:
        sub = scored.get("sub_scores") or {}
        originals: Dict[str, float] = {}
        adjustments: Dict[str, float] = {}
        for key in NORMALIZABLE_SUB_SCORES:
            abs_value = sub.get(key)
            if not isinstance(abs_value, (int, float)):
                continue
            abs_value = float(abs_value)
            mu, sigma = cohort_stats[key]
            if sigma <= 0.0:
                # Degenerate cohort: no spread to normalize against. Leave the
                # absolute score untouched rather than pulling it toward 50.
                originals[key] = round(abs_value, 2)
                adjustments[key] = 0.0
                continue
            z = (abs_value - mu) / sigma
            # Stretch sigmoid output around the cohort mean: at z=0, relative
            # equals the absolute (no shift); at z=±2, relative pulls toward
            # 88 / 12 respectively.
            relative = _sigmoid(z) * 100.0
            blended = _blend(abs_value, relative, blend=blend)
            originals[key] = round(abs_value, 2)
            adjustments[key] = round(blended - abs_value, 2)
            sub[key] = round(blended, 2)

        # Recompute the AREA score using the (potentially blended) sub-scores.
        # (Ranking/tier key off area_score; the gated site_score is refreshed
        # below only when a commercial space was validated.)
        area_score = sum(
            sub.get(k, 0.0) * w for k, w in SCORE_WEIGHTS.items()
        )
        scored["sub_scores"] = sub
        previous_area = scored.get("area_score", scored.get("site_score"))
        scored["area_score"] = round(area_score, 2)
        scored["ranking_score"] = scored["area_score"]

        # Keep a validated site_score consistent with the new area_score.
        if scored.get("site_score_status") == "validated":
            readiness = (
                (scored.get("business_feasibility") or {}).get("lease_readiness_score")
            )
            if isinstance(readiness, (int, float)):
                scored["site_score"] = round(0.5 * scored["area_score"] + 0.5 * readiness, 2)

        scored["cohort_normalization"] = {
            "applied": True,
            "blend": blend,
            "originals": originals,
            "adjustments": adjustments,
            "previous_area_score": previous_area,
            "cohort_size": len(ranked),
            "cohort_stats": {
                k: {"mean": round(mu, 2), "stdev": round(sigma, 2)}
                for k, (mu, sigma) in cohort_stats.items()
            },
        }
        summary_rows.append({
            "candidate_id": profile.get("candidate_id"),
            "previous_area_score": previous_area,
            "new_area_score": scored["area_score"],
            "adjustments": adjustments,
        })

    return summary_rows


def factor_decomposition(
    scored: Dict[str, Any],
    cohort_means: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Return per-sub-score deltas vs cohort mean, weighted by SCORE_WEIGHTS.

    Used by the report to answer "why does this candidate's site_score sit
    above/below the cohort mean?" with concrete pull-attribution.
    """
    sub = scored.get("sub_scores") or {}
    rows: List[Dict[str, Any]] = []
    for key, weight in SCORE_WEIGHTS.items():
        value = sub.get(key)
        if not isinstance(value, (int, float)):
            continue
        cohort_mean_value = cohort_means.get(key)
        if cohort_mean_value is None:
            continue
        delta = float(value) - float(cohort_mean_value)
        contribution = delta * weight
        rows.append({
            "sub_score": key,
            "value": round(float(value), 2),
            "cohort_mean": round(float(cohort_mean_value), 2),
            "delta": round(delta, 2),
            "weight": weight,
            "contribution_to_site_delta": round(contribution, 3),
        })
    rows.sort(key=lambda r: abs(r["contribution_to_site_delta"]), reverse=True)
    return rows


def _comp_attr(c: Any, key: str, default: Any) -> Any:
    """Read ``key`` from a competitor whether it's a dict or an object."""
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)


def _comp_distance(c: Any) -> float:
    """Sortable distance for a competitor; non-numeric/missing sorts last."""
    dist = _comp_attr(c, "distance_miles", None)
    return float(dist) if isinstance(dist, (int, float)) else 9999.0


def _competitor_set_for(profile: Dict[str, Any], top_n: int = 5) -> set:
    """Return the set of place_id's for this candidate's nearest N competitors."""
    competitors = profile.get("competitors") or []
    sorted_comps = sorted(competitors, key=_comp_distance)
    out: set = set()
    for c in sorted_comps[:top_n]:
        pid = _comp_attr(c, "place_id", "")
        if pid:
            out.add(str(pid))
    return out


def apply_cohort_confidence(
    ranked: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> None:
    """Write per-candidate cohort-level confidence dimensions onto scored dicts.

    Two cohort-level signals:
    - ``catchment_overlap`` — how much each candidate's top-5 competitors
      overlap with the rest of the cohort. High overlap means the cohort
      is fighting over the same competitor pool, so ranking differences
      between candidates aren't market-meaningful.
    - ``differentiation`` — coefficient of variation of site_scores in the
      cohort. Below 2% means the math can't distinguish the candidates
      regardless of input quality.

    Empty / single-candidate cohorts: leave the placeholder 100.
    """
    if len(ranked) < 2:
        return

    # ---- catchment_overlap --------------------------------------------------- #
    competitor_sets = [_competitor_set_for(p) for p, _ in ranked]
    overlap_scores: List[float] = []
    for i, my_set in enumerate(competitor_sets):
        if not my_set:
            overlap_scores.append(1.0)  # no data → no penalty
            continue
        shared_counts: List[float] = []
        for j, other_set in enumerate(competitor_sets):
            if i == j or not other_set:
                continue
            inter = my_set & other_set
            union = my_set | other_set
            shared = len(inter) / len(union) if union else 0.0
            shared_counts.append(shared)
        if not shared_counts:
            overlap_scores.append(1.0)
            continue
        avg_overlap = sum(shared_counts) / len(shared_counts)
        # avg_overlap 0 = totally distinct catchments → confidence 1.0
        # avg_overlap 0.6+ = candidates share their top-5 competitors → low.
        confidence = max(0.0, 1.0 - (avg_overlap / 0.6))
        overlap_scores.append(min(1.0, confidence))

    # ---- differentiation ---------------------------------------------------- #
    def _rank_num(s: Dict[str, Any]) -> Optional[float]:
        for key in ("area_score", "site_score"):
            v = s.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    scores = [_rank_num(s) for _, s in ranked]
    scores = [v for v in scores if v is not None]
    if len(scores) >= 2:
        mu = mean(scores)
        sigma = pstdev(scores) if len(scores) > 1 else 0.0
        # Coefficient of variation; cap at meaningful range. 2% CV is the
        # threshold below which "scores aren't really different."
        cv = (sigma / mu) if mu > 0 else 0.0
        # CV >= 0.10 → confidence 1.0; CV <= 0.02 → confidence 0.0
        differentiation = max(0.0, min(1.0, (cv - 0.02) / 0.08))
    else:
        differentiation = 1.0

    # ---- write back --------------------------------------------------------- #
    for (profile, scored), overlap in zip(ranked, overlap_scores):
        breakdown = scored.get("confidence_breakdown") or {}
        dimensions = dict(breakdown.get("dimensions") or {})
        dimensions["catchment_overlap"] = round(overlap * 100, 2)
        dimensions["differentiation"] = round(differentiation * 100, 2)
        breakdown["dimensions"] = dimensions
        _refresh_weakest_rationale(breakdown, dimensions)
        scored["confidence_breakdown"] = breakdown


def _refresh_weakest_rationale(
    breakdown: Dict[str, Any], dimensions: Dict[str, float]
) -> None:
    """Re-derive the "weakest dimensions" bullet now that the cohort-level
    dimensions exist.

    ``compute_confidence_score`` builds the rationale before
    ``catchment_overlap`` / ``differentiation`` have real values (they're
    placeholder 100 at score time), so those two could never surface as
    "weakest" even when they genuinely are. Rewrite that one bullet in place.
    """
    rationale = breakdown.get("rationale")
    if not isinstance(rationale, list) or not dimensions:
        return
    weakest = sorted(dimensions.items(), key=lambda kv: kv[1])[:2]
    new_bullet = "weakest dimensions: " + "; ".join(
        f"{k} {v:.0f}/100" for k, v in weakest
    )
    for i, bullet in enumerate(rationale):
        if isinstance(bullet, str) and bullet.startswith("weakest dimensions:"):
            rationale[i] = new_bullet
            return
    rationale.append(new_bullet)


def cohort_means_from_ranked(
    ranked: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, float]:
    """Compute per-sub-score cohort means across the ranked set."""
    out: Dict[str, float] = {}
    if not ranked:
        return out
    keys = list(SCORE_WEIGHTS.keys())
    for key in keys:
        values: List[float] = []
        for _, scored in ranked:
            sub = scored.get("sub_scores") or {}
            v = sub.get(key)
            if isinstance(v, (int, float)):
                values.append(float(v))
        if values:
            out[key] = sum(values) / len(values)
    return out
