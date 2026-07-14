"""
Demand-bucket mapper: collapse the canonical course-type taxonomy into the
three charted demand channels (ARC CPR / ARC BLS / AHA BLS) plus OTHER.

This is a THIN layer over ``app.enrichers.course_classifier`` — it never
re-classifies a class name except for one documented case (skills sessions,
which the classifier buckets before provider detection).

Locked decisions (see docs/superpowers/specs/2026-06-10-zip-demand-design.md):
  - Exactly three charted buckets. Do not add more.
  - The ARC_CPR bucket intentionally absorbs blended CPR/First Aid and AHA
    Heartsaver — it represents "general CPR / First Aid demand", but keeps the
    label "ARC CPR" for report stability. Accepted simplification.
  - ARC BLS and AHA BLS are never mixed; CPR is never BLS; BLS is never CPR.
  - Everything else (ALLCPR house brand, ACLS, PALS, providerless skills
    sessions, unknown) is OTHER and stays out of the charts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

# Deliberate coupling: we reuse the classifier's OWN private provider/normalize
# helpers so bucket mapping can never diverge from canonical classification
# rules. Keeping these private-but-imported is intentional — do not re-implement.
from app.enrichers.course_classifier import _detect_provider, _norm

ARC_CPR = "ARC_CPR"
ARC_BLS = "ARC_BLS"
AHA_BLS = "AHA_BLS"
OTHER = "OTHER"

# The three charted demand channels, in canonical display order.
REPORT_BUCKETS: Tuple[str, str, str] = (ARC_CPR, ARC_BLS, AHA_BLS)

BUCKET_LABELS: Dict[str, str] = {
    ARC_CPR: "ARC CPR",   # internally also blended + Heartsaver (documented)
    ARC_BLS: "ARC BLS",
    AHA_BLS: "AHA BLS",
    OTHER: "Other",
}

# classifier key -> bucket. skills_session is handled separately (provider
# re-check); anything absent from this map is OTHER.
_KEY_TO_BUCKET: Dict[str, str] = {
    "arc_cpr": ARC_CPR,
    "cpr_first_aid_blended": ARC_CPR,   # fold: provider-less blended CPR/FA
    "aha_cpr": ARC_CPR,                 # fold: Heartsaver counts as CPR demand
    "arc_bls": ARC_BLS,
    "aha_bls": AHA_BLS,
}

_BLS_RE = re.compile(r"\bbls\b")


def to_demand_bucket(course_type: Any, class_name: Any = "") -> str:
    """Map a canonical course-type key onto ARC_CPR/ARC_BLS/AHA_BLS/OTHER.

    ``class_name`` is consulted ONLY for skills sessions: the classifier sends
    every skills class to ``skills_session`` before provider detection, but a
    "Red Cross BLS Skills Session" is real ARC BLS demand. We reuse the
    classifier's own ``_detect_provider`` so provider rules never diverge.
    """
    key = _norm(course_type)
    if key == "skills_session":
        name = _norm(class_name)
        if _BLS_RE.search(name) or "basic life support" in name:
            provider = _detect_provider(name)
            if provider == "arc":
                return ARC_BLS
            if provider == "aha":
                return AHA_BLS
        return OTHER  # provider-less or non-BLS skills session
    return _KEY_TO_BUCKET.get(key, OTHER)


def demand_strength_category(score: Any) -> str:
    """Demand strength band for a 0..100 zip_demand_score."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "Very Weak"
    if s >= 90:
        return "Very Strong"
    if s >= 75:
        return "Strong"
    if s >= 60:
        return "Moderate"
    if s >= 40:
        return "Weak"
    return "Very Weak"
