"""
Decision logic for a selected ZIP — the "what should we do" layer.

Turns a ZIP's scores + data quality + commercial reality into a single
decision label, a plain reason, concrete next steps, and risk flags. It is
deliberately conservative: it speaks in "test / validate" language and never
says "open now" or implies a signed lease.

This is the canonical, unit-tested implementation. The dashboard mirrors the
same thresholds in JS so it can recompute live as the user switches course /
layer; keep the two in sync.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Decision labels (the only ones we emit).
STRONG = "Strong test area"
POSSIBLE = "Possible opportunity"
WATCHING = "Keep watching"
LOWER = "Lower priority"
COMMERCIAL = "Commercially promising — validate in person"

_SCORE_HIGH = 60.0
_SCORE_MID = 40.0
_COURSE_HIGH = 0.60   # normalized 0..1 selected-course value

_BASE_NEXT_STEPS = [
    "Check rent",
    "Check parking",
    "Check classroom availability",
    "Check competitor schedule/pricing",
    "Run a small demand test (calls / landing page)",
    "Validate in person",
]


def _commercial_ready(commercial: Optional[Dict[str, Any]]) -> bool:
    if not commercial or not commercial.get("commercial_validated"):
        return False
    return bool(commercial.get("commercial_ready"))


def decide(
    layer: str,
    *,
    overall_score: Optional[float],
    course_norm: Optional[float] = None,
    course_value: Optional[float] = None,
    data_confidence: Optional[str] = None,
    commercial: Optional[Dict[str, Any]] = None,
    enrichment_tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``{decision_label, decision_reason, next_steps, risk_flags}``.

    ``layer`` is ``"historical"`` or ``"modeled"``. ``course_norm`` is the
    selected course value normalized to 0..1 (historical: value/max; modeled:
    value/100). ``commercial`` is a summary from
    :mod:`app.reports.commercial_validation`.
    """
    score = overall_score if isinstance(overall_score, (int, float)) else 0.0
    cnorm = course_norm if isinstance(course_norm, (int, float)) else 0.0
    score_high = score >= _SCORE_HIGH
    score_mid = score >= _SCORE_MID
    course_high = cnorm >= _COURSE_HIGH
    risk_flags: List[str] = []

    if layer == "historical":
        if score_high and course_high:
            label, reason = STRONG, ("Strong demand score and real course "
                                     "activity for this course.")
        elif score_high and not course_high:
            label, reason = POSSIBLE, ("Solid overall demand, but limited "
                                       "historical activity for this course.")
        elif not score_high and course_high:
            label, reason = WATCHING, ("Lower overall score, yet a real "
                                       "historical niche exists for this course — "
                                       "review the model.")
        else:
            label, reason = LOWER, ("Both the demand score and historical "
                                    "activity are low.")
    else:  # modeled
        if score_high and course_high:
            label, reason = STRONG, ("High modeled opportunity and strong "
                                     "demand tilt — a public-data estimate that "
                                     "needs a field test.")
        elif score_mid:
            label, reason = POSSIBLE, ("Moderate modeled opportunity — worth a "
                                       "demand test before committing.")
        else:
            label, reason = LOWER, "Low modeled opportunity for this ZIP."
        risk_flags.append("Modeled estimate — not real enrollment; validate.")
        if enrichment_tier:
            # Enriched + weak commercial/access tempers a strong call.
            if label == STRONG and not _commercial_ready(commercial):
                reason += (" Enrichment present but no validated, available "
                           "commercial space yet — test carefully.")

    # Commercial upgrade (applies to any layer): a usable, available space turns
    # a strong-but-unvalidated call into a "go look in person" call.
    if label == STRONG and _commercial_ready(commercial):
        label = COMMERCIAL
        reason += (" A validated, available commercial space with parking and "
                   "classroom fit exists — worth an in-person visit.")

    # Risk flags.
    if data_confidence in ("missing", "partial", "Low", "None"):
        risk_flags.append("Low data confidence — interpret cautiously.")
    if not _commercial_ready(commercial):
        risk_flags.append("Commercial space not validated (rent/parking/"
                          "classroom/availability unknown).")

    return {
        "decision_label": label,
        "decision_reason": reason,
        "next_steps": list(_BASE_NEXT_STEPS),
        "risk_flags": risk_flags,
    }
