"""
Economy score — 0..100.

Combines population, income, working-age share, healthcare employment
share, and educational attainment into a single 0..100 score. Any missing
sub-component is excluded from the weighted average rather than imputed,
and contributes a missing-data penalty via the confidence scorer.

Reference ranges below are explicit policy choices, not measurements.

Missing-data behavior:
  - If NO economy fields are usable (e.g. Census fails), we return a
    NEUTRAL score (default 50.0, env-configurable) and set
    `data_confidence="missing"`. We do not deflate site_score to 0
    purely because the Census API didn't answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# Each entry: (input_field, label, low, high, weight)
# Where `low` -> 0.0 and `high` -> 1.0 in the normalized sub-score.
# `healthcare_employment_lq` comes from BLS QCEW (county healthcare-workforce
# concentration vs the national average; 1.0 = national average). It's far
# more reliably populated than the Census C24050 healthcare-share field, so
# it carries the larger healthcare weight.
COMPONENTS: List[Tuple[str, str, float, float, float]] = [
    ("median_household_income",       "median household income",      40_000,  110_000, 0.25),
    ("working_age_share",             "working-age share (16+)",          0.55,    0.80, 0.15),
    ("healthcare_employment_share",   "healthcare employment share",      0.05,    0.20, 0.10),
    ("healthcare_employment_lq",      "healthcare workforce concentration (BLS LQ)", 0.5, 1.5, 0.15),
    ("bachelors_or_higher_share",     "bachelor's-or-higher share",       0.15,    0.50, 0.15),
    ("employment_rate",               "employment rate (employed/16+)",   0.50,    0.75, 0.10),
    ("population",                    "population (place/county)",     5_000, 200_000, 0.10),
]
assert abs(sum(w for *_, w in COMPONENTS) - 1.0) < 1e-6


# Neutral fallback when no economy data is available at all.
ECONOMY_SCORE_DEFAULT = float(os.getenv("ECONOMY_SCORE_DEFAULT", "50"))


@dataclass
class EconomyBreakdown:
    score: float                # 0..100
    used_fields: List[str]
    missing_fields: List[str]
    rationale: List[str]
    data_confidence: str        # "ok" | "partial" | "missing"


def _norm(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None:
        return None
    if high <= low:
        return None
    return max(0.0, min(1.0, (value - low) / (high - low)))


def compute_economy_score(economy_block: Dict[str, object]) -> EconomyBreakdown:
    """
    `economy_block` is the dict returned by enrichers.economy.collect_economy_for_point
    (keys: census, labor, real_estate). We pull from census.values + census.indicators.
    """
    census = economy_block.get("census") or {}
    census_values = census.get("values") or {}
    census_indicators = census.get("indicators") or {}
    labor = economy_block.get("labor") or {}
    labor_values = labor.get("values") or {}
    merged: Dict[str, Optional[float]] = {}
    merged.update({k: v for k, v in census_values.items()})
    merged.update({k: v for k, v in census_indicators.items()})
    # BLS QCEW labor signals (county-level): healthcare workforce LQ etc.
    merged.update({k: v for k, v in labor_values.items() if v is not None})

    used: List[str] = []
    missing: List[str] = []
    bullets: List[str] = []

    weighted_sum = 0.0
    weight_used = 0.0

    for field, label, low, high, weight in COMPONENTS:
        raw_val = merged.get(field)
        norm = _norm(raw_val, low, high)
        if norm is None:
            missing.append(field)
            continue
        used.append(field)
        weighted_sum += norm * weight
        weight_used += weight
        if isinstance(raw_val, float) and 0 < raw_val < 1:
            shown = f"{raw_val:.1%}"
        elif isinstance(raw_val, float):
            shown = f"{raw_val:,.0f}"
        else:
            shown = str(raw_val)
        bullets.append(f"{label}: {shown}")

    if weight_used <= 0:
        # No data at all: use neutral default rather than 0 (which would
        # unfairly deflate site_score). Confidence scoring already penalizes
        # the missing fields, so this is the honest signal.
        return EconomyBreakdown(
            score=round(ECONOMY_SCORE_DEFAULT, 2),
            used_fields=[],
            missing_fields=missing,
            rationale=[f"no census/economy data available — "
                       f"using neutral default ({ECONOMY_SCORE_DEFAULT:.0f}); "
                       f"see confidence_score for data-quality signal"],
            data_confidence="missing",
        )

    score_01 = weighted_sum / weight_used
    confidence = "ok" if weight_used >= 0.7 else "partial"
    return EconomyBreakdown(
        score=round(score_01 * 100, 2),
        used_fields=used,
        missing_fields=missing,
        rationale=bullets,
        data_confidence=confidence,
    )


# --- Accessibility (very simple v1) -----------------------------------------
# We don't yet have a real OSM/transit collector, so accessibility is derived
# proxy-style from the count of demand drivers that are within 1 mile.

def _distance_points(distance: Optional[float], near: float, far: float) -> float:
    if distance is None:
        return 0.0
    if distance <= near:
        return 1.0
    if distance >= far:
        return 0.0
    return max(0.0, 1.0 - ((distance - near) / (far - near)))


def _signal_distance(accessibility: Dict[str, object], key: str) -> Optional[float]:
    signals = accessibility.get("signals") or {}
    signal = signals.get(key) or {}
    if not isinstance(signal, dict) or signal.get("status") != "detected":
        return None
    dist = signal.get("distance_miles")
    return float(dist) if isinstance(dist, (int, float)) else None


def compute_accessibility_score(
    counts_by_bucket: Dict[str, Dict[int, int]],
    accessibility: Optional[Dict[str, object]] = None,
) -> float:
    """Score accessibility from real/proxy signals when available.

    Falls back to the Phase 1 one-mile-density proxy if no accessibility block
    has been collected, preserving existing behavior for saved profiles.
    """
    one_mile_total = sum(c.get(1, 0) for c in counts_by_bucket.values())
    if not accessibility:
        score_01 = min(1.0, one_mile_total / 25.0)
        return round(score_01 * 100, 2)

    freeway = _distance_points(
        _signal_distance(accessibility, "freeway_major_road_proximity"),
        near=0.5,
        far=1.5,
    )
    transit = _distance_points(
        _signal_distance(accessibility, "transit_station_proximity"),
        near=0.5,
        far=3.0,
    )
    airport_business = _distance_points(
        _signal_distance(accessibility, "airport_business_corridor_proximity"),
        near=2.0,
        far=8.0,
    )
    shopping = _distance_points(
        _signal_distance(accessibility, "shopping_center_plaza_proximity"),
        near=0.25,
        far=1.25,
    )
    parking = 0.0
    signals = accessibility.get("signals") or {}
    parking_signal = signals.get("parking_proxy") or {}
    if isinstance(parking_signal, dict) and parking_signal.get("status") == "detected":
        parking = 0.75  # proxy, not exact parking inventory
    walkability = min(1.0, one_mile_total / 25.0)

    score_01 = (
        0.20 * freeway
        + 0.20 * transit
        + 0.15 * airport_business
        + 0.20 * shopping
        + 0.10 * parking
        + 0.15 * walkability
    )
    return round(score_01 * 100, 2)
