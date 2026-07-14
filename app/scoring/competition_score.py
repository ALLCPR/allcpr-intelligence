"""
Competition gap score — 0..100.

High when demand is real but the CPR/BLS competitor landscape is sparse or
weak (low average rating, few reviews, missing websites). This is the
"opportunity gap" — strong demand and limited competition.

Inputs:
  - demand_score_0_100:        the demand_score from demand_score.compute_demand_score
  - competition_summary:       summary dict from competition enricher

Heuristic:
  - Start from saturation = min(1.0, competitor_count_5mi / SATURATION_CAP)
  - Weakness modifier: competitors with low avg rating (< 4.0) or few reviews
    (< 25) reduce effective saturation.
  - competition_gap = (1 - effective_saturation)
  - Modulate by demand: gap matters more where demand is real. Final =
    sqrt(demand_norm * gap), then scaled to 0..100.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict, List, Optional

SATURATION_CAP = 6  # at 6+ CPR competitors within 5mi we call it saturated


@dataclass
class CompetitionBreakdown:
    score: float
    effective_saturation: float
    rationale: List[str]


def compute_competition_gap_score(
    demand_score_0_100: float,
    competition_summary: Dict[str, object],
) -> CompetitionBreakdown:
    counts_by_bucket = competition_summary.get("competitor_count_by_bucket_mi") or {}
    count_5mi: int = int(counts_by_bucket.get(5, 0) or 0)
    avg_rating: Optional[float] = competition_summary.get("competitor_avg_rating")  # type: ignore[assignment]
    total_reviews: int = int(competition_summary.get("competitor_total_reviews") or 0)
    no_website: int = int(competition_summary.get("competitor_no_website") or 0)

    raw_saturation = min(1.0, count_5mi / SATURATION_CAP) if SATURATION_CAP > 0 else 0.0

    # Weakness factor: reduce saturation if competitors look weak.
    weakness = 0.0
    if avg_rating is not None and avg_rating < 4.0:
        weakness += min(0.3, (4.0 - avg_rating) * 0.3)
    if count_5mi > 0 and total_reviews / max(count_5mi, 1) < 25:
        weakness += 0.1
    if count_5mi > 0 and no_website / max(count_5mi, 1) > 0.3:
        weakness += 0.1
    weakness = min(weakness, 0.5)

    effective_saturation = max(0.0, raw_saturation - weakness)
    gap = 1.0 - effective_saturation

    demand_norm = max(0.0, min(1.0, demand_score_0_100 / 100.0))
    blended = sqrt(demand_norm * gap)

    bullets: List[str] = []
    bullets.append(f"{count_5mi} CPR/BLS competitors within 5 mi")
    if avg_rating is not None:
        bullets.append(f"competitor avg rating {avg_rating:.2f}")
    else:
        bullets.append("competitor avg rating unknown")
    bullets.append(f"{total_reviews} total competitor reviews")
    if weakness > 0:
        bullets.append(f"weakness adjustment -{weakness:.2f} (low ratings / "
                       f"reviews / websites)")

    return CompetitionBreakdown(
        score=round(blended * 100, 2),
        effective_saturation=round(effective_saturation, 3),
        rationale=bullets,
    )
