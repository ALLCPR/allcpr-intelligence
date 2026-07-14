"""
Competitor operational-scale signals.

Pure competitor counts overestimate or underestimate saturation. One large
AHA training center with hundreds of reviews and full ACLS/PALS offerings is
not equivalent to a one-instructor CPR side-hustle with five reviews — but
counted equally in ``competition_summary.competitor_count_total``.

This module turns the existing aggregates (review counts, scale bands,
weakness flags) into a small set of derived metrics the report can show
alongside the raw saturation gap:

- ``competition_pressure_score`` (0..100): review-weighted, scale-aware
  estimate of how hard the local market is to enter. High = crowded with
  established providers; low = sparse or weak operators only.
- ``dominant_provider_index`` (0..1): share of total competitor reviews
  captured by the single highest-review competitor — proxy for incumbent
  dominance.
- ``estimated_market_capacity``: rough total review volume across all
  competitors (a stand-in for "served class throughput"). Pure proxy.
- ``demand_to_competition_ratio``: demand_score / max(1, pressure_score).
  Above ~1.0 suggests demand is outrunning current supply.

Every metric falls back to ``None`` when the underlying counts are zero
or unknown; nothing is fabricated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CompetitionPressureBreakdown:
    competition_pressure_score: Optional[float]
    dominant_provider_index: Optional[float]
    estimated_market_capacity: Optional[int]
    demand_to_competition_ratio: Optional[float]
    rationale: List[str]


def _i(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _f(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compute_competition_pressure(
    competition_summary: Dict[str, object],
    demand_score_0_100: float,
) -> CompetitionPressureBreakdown:
    total = _i(competition_summary.get("competitor_count_total"))
    if total <= 0:
        return CompetitionPressureBreakdown(
            competition_pressure_score=None,
            dominant_provider_index=None,
            estimated_market_capacity=None,
            demand_to_competition_ratio=None,
            rationale=["no competitors found — pressure unknown"],
        )

    total_reviews = _i(competition_summary.get("competitor_total_reviews"))
    top_reviews = _i(competition_summary.get("competitor_top_reviews"))
    large = _i(competition_summary.get("competitor_scale_large"))
    medium = _i(competition_summary.get("competitor_scale_medium"))
    small = _i(competition_summary.get("competitor_scale_small"))
    unknown = _i(competition_summary.get("competitor_scale_unknown"))

    # Pressure components, each capped at 1.0.
    # 1. Density component: count of competitors normalized by SATURATION_CAP.
    density = min(1.0, total / 6.0)
    # 2. Scale component: each large counts 3x, medium 1.5x, small 1x.
    scale_weight = (3.0 * large + 1.5 * medium + 1.0 * small) / 6.0
    scale = min(1.0, scale_weight)
    # 3. Reach component: total review volume saturates around 800 reviews.
    reach = min(1.0, total_reviews / 800.0) if total_reviews > 0 else 0.0

    # Subtract weakness (low ratings / missing booking / etc.).
    low_rated = _i(competition_summary.get("competitor_low_rating_count"))
    no_site = _i(competition_summary.get("competitor_no_website"))
    booking_missing = _i(competition_summary.get("competitor_online_booking_missing"))
    weakness_share = min(
        1.0,
        (1.5 * low_rated + no_site + booking_missing) / max(3.0 * total, 1.0),
    )
    weakness_dampener = 0.4 * weakness_share

    pressure_norm = max(
        0.0,
        (0.4 * density + 0.4 * scale + 0.2 * reach) - weakness_dampener,
    )
    pressure_score = round(pressure_norm * 100.0, 2)

    dominant = round(top_reviews / total_reviews, 3) if total_reviews > 0 else None

    capacity = total_reviews if total_reviews > 0 else None

    demand_norm = max(0.0, min(1.0, (demand_score_0_100 or 0.0) / 100.0))
    if pressure_norm > 0:
        ratio = round(demand_norm / pressure_norm, 3)
    elif demand_norm > 0:
        ratio = 9.999  # demand without measurable competition
    else:
        ratio = None

    bullets: List[str] = []
    bullets.append(
        f"{total} competitors ({large} large / {medium} medium / "
        f"{small} small / {unknown} unknown by review band)"
    )
    if total_reviews > 0:
        bullets.append(f"{total_reviews} total reviews across competitors")
    if dominant is not None and dominant >= 0.4:
        bullets.append(
            f"single competitor holds {dominant:.0%} of total reviews "
            f"(dominant incumbent)"
        )
    if weakness_share > 0.25:
        bullets.append(
            f"weakness dampener -{weakness_dampener:.2f} (low ratings / "
            f"missing websites / no booking flow)"
        )
    if ratio is not None and ratio >= 1.0:
        bullets.append(
            f"demand-to-competition ratio {ratio:.2f} — demand may outrun supply"
        )

    return CompetitionPressureBreakdown(
        competition_pressure_score=pressure_score,
        dominant_provider_index=dominant,
        estimated_market_capacity=capacity,
        demand_to_competition_ratio=ratio,
        rationale=bullets,
    )
