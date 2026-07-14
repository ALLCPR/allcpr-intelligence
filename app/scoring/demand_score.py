"""
Demand score — 0..100.

Reflects how much CPR/BLS demand a candidate location is likely to see based
on the count of nearby demand drivers (hospitals, fire stations, EMS,
healthcare schools, colleges, childcare, eldercare, clinics).

Design choices:
  - Each category contributes via a saturating curve. Going from 0 -> 1 nearby
    hospital is far more important than 9 -> 10. We use a log-style transform
    `min(1.0, count / cap)` with a per-category cap calibrated so a healthy
    metro neighborhood lands near 1.0.
  - Each category has a weight reflecting how strongly it drives CPR demand
    (hospitals/EMS > gyms).
  - The final score is the weighted sum, scaled to 0..100.

These caps and weights are documented and can be tuned. They are not derived
from data — they are explicit policy choices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from app.config import DEMAND_CAP_MULTIPLIER

# (category_key, cap_at_5mi, weight)
# Caps calibrated so a dense urban metro (SF, NYC, LA) does NOT insta-saturate.
# Previous caps (hospital=4, nursing_school=3, etc.) maxed out at the lowest
# end of urban density, collapsing inter-candidate score differentiation.
DEMAND_CATEGORY_PARAMS: List[Tuple[str, float, float]] = [
    ("hospital",           10,  0.18),
    ("urgent_care",        10,  0.06),
    ("fire_station",        8,  0.12),
    ("ems",                 6,  0.08),
    ("nursing_school",      8,  0.10),
    ("medical_school",      6,  0.05),
    ("dental_school",       4,  0.03),
    ("community_college",   8,  0.06),
    ("university",          8,  0.04),
    ("childcare_center",   25,  0.07),
    ("senior_care",        15,  0.06),
    ("gym",                25,  0.03),
    ("physical_therapy",   20,  0.03),
    ("dental_clinic",      30,  0.02),
    ("medical_clinic",     25,  0.02),
    ("emt_training",        4,  0.02),
    ("cna_training",        4,  0.02),
    ("healthcare_training", 4,  0.01),
]

# Total weights should sum to ~1.0
assert abs(sum(w for _, _, w in DEMAND_CATEGORY_PARAMS) - 1.0) < 1e-6, (
    "Demand category weights must sum to 1.0"
)


@dataclass
class DemandBreakdown:
    score: float                     # 0..100
    by_category: Dict[str, float]    # per-category normalized contribution 0..1
    rationale: List[str]             # human-readable bullets


def compute_demand_score(counts_5mi: Dict[str, int]) -> DemandBreakdown:
    """
    `counts_5mi` is the 5-mile count for each demand category (from
    area_profile.counts_5mi). Missing keys are treated as 0 (Google Places
    returning zero hits is a legitimate "we looked and found none", not
    "unknown"). Genuinely-unknown demand can never reach here unless the
    Places API itself failed; the confidence score is what penalizes that.
    """
    by_category: Dict[str, float] = {}
    score_01 = 0.0
    bullets: List[str] = []
    for key, cap, weight in DEMAND_CATEGORY_PARAMS:
        count = int(counts_5mi.get(key, 0) or 0)
        eff_cap = cap * DEMAND_CAP_MULTIPLIER
        normalized = min(1.0, count / eff_cap) if eff_cap > 0 else 0.0
        by_category[key] = normalized
        score_01 += normalized * weight
        if count > 0:
            bullets.append(f"{count} {key.replace('_', ' ')} within 5 mi")
    return DemandBreakdown(
        score=round(score_01 * 100, 2),
        by_category=by_category,
        rationale=bullets,
    )


# --- Healthcare training ecosystem sub-score ---------------------------------
# Same shape as demand but only across the training-ecosystem keys, on a
# higher cap so that a thin ecosystem doesn't max out the sub-score.

TRAINING_PARAMS: List[Tuple[str, float, float]] = [
    ("nursing_school",      8, 0.22),
    ("medical_school",      6, 0.12),
    ("dental_school",       4, 0.06),
    ("community_college",   8, 0.18),
    ("university",          8, 0.14),
    ("emt_training",        4, 0.10),
    ("cna_training",        4, 0.10),
    ("healthcare_training", 4, 0.08),
]
assert abs(sum(w for _, _, w in TRAINING_PARAMS) - 1.0) < 1e-6


def compute_training_ecosystem_score(counts_5mi: Dict[str, int]) -> DemandBreakdown:
    by_category: Dict[str, float] = {}
    score_01 = 0.0
    bullets: List[str] = []
    for key, cap, weight in TRAINING_PARAMS:
        count = int(counts_5mi.get(key, 0) or 0)
        normalized = min(1.0, count / cap) if cap > 0 else 0.0
        by_category[key] = normalized
        score_01 += normalized * weight
        if count > 0:
            bullets.append(f"{count} {key.replace('_', ' ')} within 5 mi")
    return DemandBreakdown(
        score=round(score_01 * 100, 2),
        by_category=by_category,
        rationale=bullets,
    )
