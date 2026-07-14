"""
Profitability estimator — explicitly labeled ESTIMATED.

Builds a low/mid/high band for monthly students and monthly revenue from
configurable assumptions in `app.config`. The output is NEVER presented as
fact — every report row that uses it carries the word "estimated".

Inputs are normalized scores; we don't fabricate Census income or rent here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.collectors import allcpr_prices
from app.config import (
    AVG_STUDENTS_PER_CLASS,
    CLASSES_PER_WEEK_HIGH,
    CLASSES_PER_WEEK_LOW,
    CLASSES_PER_WEEK_MID,
)

WEEKS_PER_MONTH = 4.33  # approx


@dataclass
class ProfitabilityEstimate:
    score: float                       # 0..100 normalized for use in site_score
    students_low: int
    students_mid: int
    students_high: int
    revenue_low: float
    revenue_mid: float
    revenue_high: float
    avg_course_price: float
    price_source: str = "config_default"   # provenance of avg_course_price
    price_sample_size: int = 0             # n behind the price (0=config_default)
    notes: List[str] = None
    confidence: str = "estimated"      # never "official"

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def _utilization_factor(opportunity_score_0_100: float,
                        demand_score_0_100: float) -> float:
    """0..1 multiplier: how many of the budgeted classes are likely to fill."""
    opp = max(0.0, min(1.0, opportunity_score_0_100 / 100.0))
    dem = max(0.0, min(1.0, demand_score_0_100 / 100.0))
    # Weighted: opportunity matters more for fill rate than raw demand.
    return 0.4 + 0.4 * opp + 0.2 * dem  # 0.4..1.0 ceiling


def estimate_profitability(
    opportunity_score_0_100: float,
    demand_score_0_100: float,
    training_score_0_100: float,
    state: str = "",
) -> ProfitabilityEstimate:
    # Prefer ALLCPR's actual observed prices over generic config defaults
    # when we have a reliable state median; otherwise overall median; else
    # legacy config $90 default. Source is recorded in price_source.
    price_lookup = allcpr_prices.lookup_price(state)
    avg_price = price_lookup.avg_price
    util = _utilization_factor(opportunity_score_0_100, demand_score_0_100)

    # Students per month = classes/week * weeks/month * students/class * util.
    def students(classes_per_week: float) -> int:
        return int(round(classes_per_week * WEEKS_PER_MONTH
                         * AVG_STUDENTS_PER_CLASS * util))

    s_low = students(CLASSES_PER_WEEK_LOW)
    s_mid = students(CLASSES_PER_WEEK_MID)
    s_high = students(CLASSES_PER_WEEK_HIGH)

    r_low = round(s_low * avg_price, 2)
    r_mid = round(s_mid * avg_price, 2)
    r_high = round(s_high * avg_price, 2)

    # Profitability sub-score: normalize the mid scenario against a soft
    # ceiling of $40k/mo revenue (representative for a single training center).
    norm = min(1.0, r_mid / 40_000.0)
    score = round(norm * 100, 2)

    src = price_lookup.source
    src_note = {
        "config_default": "config default (no ALLCPR price data loaded)",
        "overall_median": f"ALLCPR overall median across {price_lookup.sample_size} class records",
    }.get(src, f"ALLCPR {src} median across {price_lookup.sample_size} class records")
    notes = [
        f"avg course price: ${avg_price:.0f}  ({src_note})",
        f"estimated utilization factor: {util:.0%} of capacity",
        f"assumes {AVG_STUDENTS_PER_CLASS:.0f} students/class, "
        f"{CLASSES_PER_WEEK_LOW:.0f}/{CLASSES_PER_WEEK_MID:.0f}/"
        f"{CLASSES_PER_WEEK_HIGH:.0f} classes/wk for low/mid/high",
        "all figures are model-based estimates, not measurements",
    ]

    return ProfitabilityEstimate(
        score=score,
        students_low=s_low,
        students_mid=s_mid,
        students_high=s_high,
        revenue_low=r_low,
        revenue_mid=r_mid,
        revenue_high=r_high,
        avg_course_price=avg_price,
        price_source=src,
        price_sample_size=price_lookup.sample_size,
        notes=notes,
    )
