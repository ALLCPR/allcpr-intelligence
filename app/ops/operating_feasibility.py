"""
Operating feasibility: "Can ALLCPR actually run classes in this ZIP?"

Combines the existing demand/site-priority scores (never recomputed here)
with the new instructor-supply and classroom-supply readiness into one
operator-facing answer: a 0..100 feasibility score plus a recommended action,
missing requirements, risk flags, a plain-English explanation, and next steps.

Formulas (weights per operations spec):
    Overall / ARC:  40% demand + 25% instructor + 25% classroom
                    + 10% historical/competitor proof − penalties
    AHA BLS:        35% demand + 40% AHA instructor + 15% classroom
                    + 10% healthcare/competitor proof − penalties

AHA BLS weights instructor readiness heavier because the AHA instructor /
Training Center relationship is the hardest bottleneck.

SOP-derived operational rules used as logic (no private details):
    * open-site test threshold: a test class "performs" when one week gets
      more than 5 signups or total signups reach 10;
    * site decision bands: 85+ priority candidate, 75–84.9 management review,
      65–74.9 hold/compare, under 65 reject — used to gate the
      permanent-center recommendation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.ops.instructor_supply import (
    instructor_readiness_score,
    top_instructor_leads,
)
from app.ops.models import (
    AHA_BLS,
    ARC_BLS,
    ARC_CPR_FA_AED,
    COURSE_LABELS,
    COURSE_TYPES,
    OVERALL,
    READINESS_COURSE_TYPES,
    RECOMMENDED_ACTION_LABELS,
    STAGE_CANDIDATE_FOUND,
    STAGE_CONFIRMED,
    ZipOperatingReadiness,
    band_label,
    is_signal_lead,
)
from app.ops.local_market import local_market_context
from app.ops.recruiting_policy import priority_market_for_zip
from app.ops.space_supply import classroom_readiness_score, top_space_leads
from app.ops.unit_economics import site_economics

# Demand below this is "weak" for operating purposes; between weak and good is
# borderline (research). Aligned with the dashboard's Medium band floor.
WEAK_DEMAND_THRESHOLD = 40.0
GOOD_DEMAND_THRESHOLD = 55.0

# Readiness levels at which a leg stops being the blocker.
INSTRUCTOR_READY_THRESHOLD = 80.0   # confirmed or existing ALLCPR nearby
CLASSROOM_READY_THRESHOLD = 80.0    # confirmed or multiple likely rooms

# Break-even reality check: when real local demand (6-mo Enrollware students)
# is below the dollar break-even for the easiest course, feasibility takes a
# capped penalty proportional to the shortfall. Unknown economics = 0 penalty
# (honest neutral), so ZIPs without demand/cost data are unaffected.
ECON_MAX_PENALTY = 15.0

# SOP open-site test threshold.
TEST_WEEKLY_SIGNUPS_PASS = 5    # "more than 5 signups" in one week
TEST_TOTAL_SIGNUPS_PASS = 10    # or total signups reach 10

# SOP site decision band for a permanent-center candidate.
PERMANENT_CENTER_MIN_SCORE = 85.0

_WEIGHTS = {
    # course: (demand, instructor, classroom, proof)
    OVERALL: (0.40, 0.25, 0.25, 0.10),
    ARC_BLS: (0.40, 0.25, 0.25, 0.10),
    ARC_CPR_FA_AED: (0.40, 0.25, 0.25, 0.10),
    AHA_BLS: (0.35, 0.40, 0.15, 0.10),
}

_COURSE_DEMAND_KEYS = {
    AHA_BLS: ("aha_bls_score", "bls_demand"),
    ARC_BLS: ("arc_bls_score", "bls_demand"),
    ARC_CPR_FA_AED: ("arc_cpr_score", "cpr_demand"),
    OVERALL: (),
}


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def demand_score_for_course(zip_row: Dict[str, Any], course: str
                            ) -> Optional[float]:
    """Pull the demand score from the existing engines, course-aware.

    Prefers v2.1 ``market_demand_score``, blended toward the course-specific
    modeled score when one exists; falls back through the legacy fields.
    """
    base = _as_float(zip_row.get("market_demand_score"))
    if base is None:
        base = _as_float(zip_row.get("demand_score"))
    if base is None:
        base = _as_float(zip_row.get("overall"))
    if base is None:
        base = _as_float(zip_row.get("overall_score"))
    course_score = None
    for key in _COURSE_DEMAND_KEYS.get(course, ()):
        course_score = _as_float(zip_row.get(key))
        if course_score is not None:
            break
    if base is None:
        return course_score
    if course_score is None:
        return round(base, 1)
    return round(0.5 * base + 0.5 * course_score, 1)


def proof_score(zip_row: Dict[str, Any]) -> Optional[float]:
    """Historical / competitor market proof (the 10% leg)."""
    if zip_row.get("historical_status") == "has_allcpr_history":
        proven = _as_float(zip_row.get("proven_demand_score"))
        if proven is not None:
            return proven
        return 75.0  # history exists but score not computed — solid proof
    competitor = _as_float(zip_row.get("competitor_market_validation_score"))
    if competitor is not None:
        return competitor
    return None


def penalties(zip_row: Dict[str, Any]) -> Dict[str, float]:
    """Saturation / cannibalization penalties from existing annotations."""
    saturation = _as_float(zip_row.get("competition_saturation_penalty")) or 0.0
    cannibalization = _as_float(zip_row.get("cannibalization_risk")) or 0.0
    return {
        "competition_saturation_penalty": round(saturation, 1),
        "cannibalization_risk": round(cannibalization, 1),
        "total": round(saturation + cannibalization, 1),
    }


def economics_penalty(economics: Optional[Dict[str, Any]]
                      ) -> Tuple[float, Optional[str]]:
    """(penalty 0..ECON_MAX_PENALTY, human note) from unit-economics data.

    Uses ``demand_read`` (real 6-mo student demand vs the easiest course's
    dollar break-even). Full coverage or unknown data → 0. A ZIP at 11% of
    break-even (e.g. 95112: ~4 students/mo vs ~37 needed) gets close to the
    full penalty — feasibility should not look healthy on demand the dollars
    can't support.
    """
    read = (economics or {}).get("demand_read") or {}
    pct = _as_float(read.get("demand_vs_break_even_pct"))
    if pct is None:
        return 0.0, None
    coverage = max(0.0, min(1.0, pct / 100.0))
    if coverage >= 1.0:
        return 0.0, None
    penalty = round((1.0 - coverage) * ECON_MAX_PENALTY, 1)
    note = (f"Real local demand covers only {pct:.0f}% of break-even "
            f"({read.get('local_students_per_month')} students/mo vs "
            f"{read.get('easiest_break_even_students_per_month')} needed) — "
            "revenue risk")
    return penalty, note


def _weighted_feasibility(course: str,
                          demand: Optional[float],
                          instructor: float,
                          classroom: float,
                          proof: Optional[float],
                          penalty_total: float) -> Optional[float]:
    if demand is None:
        return None
    w_demand, w_inst, w_room, w_proof = _WEIGHTS[course]
    proof_val = proof if proof is not None else 50.0  # neutral, house style
    raw = (demand * w_demand + instructor * w_inst + classroom * w_room
           + proof_val * w_proof) - penalty_total
    return round(max(0.0, min(100.0, raw)), 1)


def _test_class_passed(test_class: Optional[Dict[str, Any]]) -> bool:
    if not test_class:
        return False
    weekly = _as_float(test_class.get("best_week_signups")) or 0
    total = _as_float(test_class.get("total_signups")) or 0
    return weekly > TEST_WEEKLY_SIGNUPS_PASS or total >= TEST_TOTAL_SIGNUPS_PASS


def recommend_action(
    course: str,
    demand: Optional[float],
    instructor: Dict[str, Any],
    classroom: Dict[str, Any],
    feasibility: Optional[float],
    risk_flags: List[str],
    test_class: Optional[Dict[str, Any]] = None,
) -> str:
    """Operator decision ladder (order matters — worst blocker first)."""
    inst_score = instructor.get("score") or 0.0
    room_score = classroom.get("score") or 0.0

    if demand is None:
        return "RESEARCH_NEEDED"
    if demand < WEAK_DEMAND_THRESHOLD:
        return "NOT_READY_DEMAND_WEAK"
    if inst_score <= 0:
        return "NOT_READY_NO_INSTRUCTOR"
    if inst_score < INSTRUCTOR_READY_THRESHOLD:
        return "INSTRUCTOR_OUTREACH_NEEDED"
    if room_score <= 0:
        return "NOT_READY_NO_SPACE"
    if room_score < CLASSROOM_READY_THRESHOLD:
        return "SPACE_OUTREACH_NEEDED"
    # Demand + instructor + room all confirmed/ready.
    if _test_class_passed(test_class):
        if (feasibility is not None
                and feasibility >= PERMANENT_CENTER_MIN_SCORE
                and not risk_flags):
            return "PERMANENT_CENTER_CANDIDATE"
        return "RECURRING_CLASS_CANDIDATE"
    if demand < GOOD_DEMAND_THRESHOLD:
        # Ready to operate but demand is only borderline — validate first.
        return "RESEARCH_NEEDED"
    return "TEST_CLASS_READY"


def _missing_requirements(course: str,
                          demand: Optional[float],
                          instructor: Dict[str, Any],
                          classroom: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if demand is None:
        missing.append("Demand score not yet modeled for this ZIP")
    elif demand < WEAK_DEMAND_THRESHOLD:
        missing.append("Sufficient demand (score is weak)")
    inst_score = instructor.get("score") or 0.0
    if inst_score < INSTRUCTOR_READY_THRESHOLD:
        label = COURSE_LABELS.get(course, course)
        if inst_score <= 0:
            missing.append(f"Any instructor path for {label}")
        elif inst_score < 65:
            missing.append(f"Named {label} instructor candidate "
                           "(only signals exist)")
        else:
            missing.append(f"Verified, available {label} instructor "
                           "(candidates found, not confirmed)")
    room_score = classroom.get("score") or 0.0
    if room_score < CLASSROOM_READY_THRESHOLD:
        if room_score <= 0:
            missing.append("Any classroom/shared-space path")
        elif room_score < 60:
            missing.append("Specific room candidate (only weak/limited "
                           "room supply)")
        else:
            missing.append("Confirmed recurring room (rooms found, CPR fit "
                           "or booking not verified)")
    return missing


def _risk_flags(zip_row: Dict[str, Any],
                classroom_counts: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    risk_level = zip_row.get("competition_risk_level")
    if risk_level in ("competitive", "saturated_unless_differentiated"):
        flags.append(f"competition_{risk_level}")
    if zip_row.get("historical_status") == "no_allcpr_history":
        flags.append("no_allcpr_history")
    if (classroom_counts or {}).get("eliminated"):
        flags.append(
            f"{classroom_counts['eliminated']} space lead(s) hard-eliminated")
    cannibalization = _as_float(zip_row.get("cannibalization_risk")) or 0.0
    if cannibalization > 0:
        flags.append("cannibalization_risk_with_existing_allcpr_sites")
    return flags


def _next_steps(action: str, course: str,
                instructor: Dict[str, Any],
                classroom: Dict[str, Any]) -> List[str]:
    label = COURSE_LABELS.get(course, course)
    inst_counts = instructor.get("counts") or {}
    steps: List[str] = []
    if action == "NOT_READY_DEMAND_WEAK":
        steps.append("Hold this ZIP — demand does not support a class yet.")
        steps.append("Re-check after the next national demand refresh, or "
                     "compare against nearby stronger ZIPs.")
    elif action == "RESEARCH_NEEDED":
        steps.append("Demand is unmodeled or borderline — run/refresh "
                     "enrichment for this ZIP before spending outreach time.")
    elif action in ("NOT_READY_NO_INSTRUCTOR", "INSTRUCTOR_OUTREACH_NEEDED"):
        named = inst_counts.get("named_candidates") or 0
        if named:
            steps.append(f"Contact top {min(named, 5)} instructor "
                         f"candidate(s) for {label}.")
        else:
            steps.append(f"Source {label} instructor candidates: nursing "
                         "programs, EMT/fire academies, hospital educators, "
                         "local CPR businesses.")
        steps.append("Verify credentials, expiration dates, rate, "
                     "availability, and recurring teaching interest before "
                     "marking anyone confirmed.")
        if course == AHA_BLS:
            steps.append("Confirm the AHA Training Center relationship "
                         "before scheduling any AHA BLS class.")
        steps.append("Also line up space leads in parallel so a confirmed "
                     "instructor is not left waiting.")
    elif action in ("NOT_READY_NO_SPACE", "SPACE_OUTREACH_NEEDED"):
        steps.append("Contact top 3 shared-space candidates.")
        steps.append("Verify room floor space, weekend/evening access, "
                     "price, and permission for CPR training equipment.")
        steps.append("Check the hard-elimination list: access control, "
                     "camera install, Wi-Fi, safe entry, recurring booking.")
    elif action == "TEST_CLASS_READY":
        steps.append("Post a test class (SOP: run CPS ads 3 days and "
                     "Google Ads 7 days).")
        steps.append("Track signups: more than 5 in a week or 10 total "
                     "means the site may open.")
        steps.append("Confirm instructor date commitment and room booking "
                     "in writing before ads go live.")
    elif action == "RECURRING_CLASS_CANDIDATE":
        steps.append("Test class passed the SOP threshold — schedule a "
                     "recurring monthly class.")
        steps.append("Lock a recurring room agreement and instructor "
                     "schedule; move to BD review.")
    elif action == "PERMANENT_CENTER_CANDIDATE":
        steps.append("Escalate to management review as a permanent-center "
                     "candidate (score in the 85+ priority band).")
        steps.append("Run the full field assessment and site checklist "
                     "before any lease commitment.")
    return steps


def _explanation(course: str, action: str,
                 demand: Optional[float],
                 instructor: Dict[str, Any],
                 classroom: Dict[str, Any]) -> str:
    label = COURSE_LABELS.get(course, course)
    parts: List[str] = []
    parts.append(f"Demand is {band_label(demand).lower()}"
                 + (f" ({demand:.0f})" if demand is not None else
                    " (not yet modeled)"))
    parts.append(f"instructor readiness for {label} is "
                 f"{(instructor.get('label') or 'No Signal').lower()} — "
                 f"{instructor.get('reason', '')}")
    parts.append(f"classroom readiness is "
                 f"{(classroom.get('label') or 'No Signal').lower()} — "
                 f"{classroom.get('reason', '')}")
    sentence = "; ".join(parts) + "."
    tail = RECOMMENDED_ACTION_LABELS.get(action, action)
    return f"{sentence} Recommended action: {tail}."


NOT_READY_NOTICE = (
    "Not ready for class posting yet — current result is based on "
    "institutional signals only. Import the real instructor roster / room "
    "list or run lead sourcing to convert signals into actionable leads.")


def action_checklist(instructor_candidates: List[Dict[str, Any]],
                     space_candidates: List[Dict[str, Any]],
                     course: str = OVERALL) -> List[Dict[str, Any]]:
    """The concrete facts still missing before a class can be posted.

    Each item is checked off only by a real (named, non-signal) lead with the
    fact recorded — signals never satisfy a checklist item.
    """
    named_inst = [c for c in instructor_candidates
                  if not is_signal_lead(c)
                  and c.get("credential_status") != "REJECTED"
                  and c.get("outreach_status") != "REJECTED"]
    if course in COURSE_TYPES:
        named_inst = [c for c in named_inst
                      if course in (c.get("courses_possible") or [])]
    verified = [c for c in named_inst
                if c.get("credential_status") == "VERIFIED"]
    rate_known = [c for c in named_inst
                  if str(c.get("rate_notes") or "").strip()]
    avail_known = [c for c in named_inst
                   if str(c.get("availability_notes") or "").strip()]

    named_rooms = [s for s in space_candidates
                   if not is_signal_lead(s)
                   and not s.get("hard_elimination_flags")
                   and s.get("outreach_status") != "REJECTED"]
    price_known = [s for s in named_rooms
                   if s.get("hourly_rate") is not None
                   or s.get("daily_rate") is not None]
    access_known = [s for s in named_rooms
                    if s.get("weekend_available") is True
                    or s.get("evening_available") is True]
    training_ok = [s for s in named_rooms
                   if s.get("training_use_allowed") is True]
    recurring_ok = [s for s in named_rooms
                    if s.get("recurring_available") is True]

    items = (
        ("named_instructor_candidate", "Named instructor candidate",
         bool(named_inst)),
        ("verified_instructor_credential", "Verified instructor credential",
         bool(verified)),
        ("instructor_rate_confirmed", "Instructor rate confirmed",
         bool(rate_known)),
        ("instructor_availability_confirmed",
         "Instructor availability confirmed", bool(avail_known)),
        ("specific_room_candidate", "Specific room candidate",
         bool(named_rooms)),
        ("room_price_confirmed", "Room price confirmed", bool(price_known)),
        ("weekend_evening_access_confirmed",
         "Weekend/evening access confirmed", bool(access_known)),
        ("training_use_allowed", "CPR/BLS training use allowed",
         bool(training_ok)),
        ("recurring_booking_possible", "Recurring booking possible",
         bool(recurring_ok)),
    )
    return [{"key": key, "label": label, "done": done}
            for key, label, done in items]


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------
def compute_course_readiness(
    zip_code: str,
    zip_row: Dict[str, Any],
    instructor_candidates: List[Dict[str, Any]],
    space_candidates: List[Dict[str, Any]],
    course: str = OVERALL,
    test_class: Optional[Dict[str, Any]] = None,
    economics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Full ZipOperatingReadiness record (as a dict) for one course."""
    course_filter = None if course == OVERALL else course
    instructor = instructor_readiness_score(instructor_candidates,
                                            course=course_filter)
    aha = instructor_readiness_score(instructor_candidates, course=AHA_BLS)
    arc_pool = [c for c in instructor_candidates
                if set(c.get("courses_possible") or [])
                & {ARC_BLS, ARC_CPR_FA_AED}]
    arc = instructor_readiness_score(arc_pool)
    classroom = classroom_readiness_score(space_candidates)
    demand = demand_score_for_course(zip_row, course)
    proof = proof_score(zip_row)
    pens = penalties(zip_row)
    econ_pen, econ_note = economics_penalty(economics)
    if econ_pen:
        pens["below_break_even_penalty"] = econ_pen
        pens["total"] = round(pens["total"] + econ_pen, 1)
    feasibility = _weighted_feasibility(
        course, demand, instructor.get("score") or 0.0,
        classroom.get("score") or 0.0, proof, pens["total"])
    risk = _risk_flags(zip_row, classroom.get("counts") or {})
    if econ_note:
        risk.append(econ_note)
    action = recommend_action(course, demand, instructor, classroom,
                              feasibility, risk, test_class)
    record = ZipOperatingReadiness(
        zip=str(zip_code).zfill(5),
        course_type=course,
        demand_score=demand,
        instructor_readiness_score=instructor.get("score"),
        aha_instructor_readiness_score=aha.get("score"),
        arc_instructor_readiness_score=arc.get("score"),
        classroom_readiness_score=classroom.get("score"),
        commercial_feasibility_score=_as_float(
            zip_row.get("commercial_feasibility_score_used")
            or zip_row.get("commercial_feasibility_score")),
        cannibalization_risk=pens["cannibalization_risk"],
        final_operating_feasibility_score=feasibility,
        recommended_action=action,
        recommended_action_label=RECOMMENDED_ACTION_LABELS.get(action, action),
        missing_requirements=_missing_requirements(
            course, demand, instructor, classroom),
        risk_flags=risk,
        explanation=_explanation(course, action, demand, instructor,
                                 classroom),
        next_steps=_next_steps(action, course, instructor, classroom),
    ).to_dict()
    # Boss-friendly labels + the readiness detail the dashboard renders.
    record["demand_label"] = band_label(demand)
    record["instructor_readiness"] = instructor
    record["classroom_readiness"] = classroom
    record["penalties"] = pens
    record["historical_competitor_proof_score"] = proof
    # Lead-stage honesty: what actually backs each readiness score.
    record["instructor_readiness_stage"] = instructor.get("stage")
    record["aha_instructor_readiness_stage"] = aha.get("stage")
    record["arc_instructor_readiness_stage"] = arc.get("stage")
    record["classroom_readiness_stage"] = classroom.get("stage")
    record["action_checklist"] = action_checklist(
        instructor_candidates, space_candidates, course=course)
    real_leads = (STAGE_CONFIRMED, STAGE_CANDIDATE_FOUND)
    signals_only = (instructor.get("stage") not in real_leads
                    or classroom.get("stage") not in real_leads)
    record["signals_only"] = signals_only
    record["not_ready_notice"] = NOT_READY_NOTICE if signals_only else ""
    return record


def build_zip_operating_readiness(
    zip_code: str,
    zip_row: Dict[str, Any],
    instructor_candidates: List[Dict[str, Any]],
    space_candidates: List[Dict[str, Any]],
    test_class: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Combined per-ZIP payload: all four course readiness records + top
    leads, shaped for the dashboard and ``GET /api/ops/zip/{zip}/readiness``.
    """
    zip5 = str(zip_code).zfill(5)
    market = local_market_context(zip5)
    economics = site_economics(
        zip5,
        competitor_ctx=market.get("competitor"),
        demand_ctx=market.get("demand"))
    courses = {
        course: compute_course_readiness(
            zip_code, zip_row, instructor_candidates, space_candidates,
            course=course, test_class=test_class, economics=economics)
        for course in READINESS_COURSE_TYPES
    }
    overall = courses[OVERALL]
    named_instructors = [c for c in instructor_candidates
                         if not is_signal_lead(c)]
    instructor_signals = [c for c in instructor_candidates
                          if is_signal_lead(c)]
    named_spaces = [s for s in space_candidates if not is_signal_lead(s)]
    space_signals = [s for s in space_candidates if is_signal_lead(s)]
    priority_city = priority_market_for_zip(zip5)
    return {
        "zip": str(zip_code).zfill(5),
        "summary": {
            "demand_label": overall["demand_label"],
            "demand_score": overall["demand_score"],
            "aha_instructor_readiness_label": band_label(
                overall["aha_instructor_readiness_score"]),
            "aha_instructor_readiness_score":
                overall["aha_instructor_readiness_score"],
            "aha_instructor_readiness_stage":
                overall["aha_instructor_readiness_stage"],
            "arc_instructor_readiness_label": band_label(
                overall["arc_instructor_readiness_score"]),
            "arc_instructor_readiness_score":
                overall["arc_instructor_readiness_score"],
            "arc_instructor_readiness_stage":
                overall["arc_instructor_readiness_stage"],
            "classroom_readiness_label": band_label(
                overall["classroom_readiness_score"]),
            "classroom_readiness_score": overall["classroom_readiness_score"],
            "classroom_readiness_stage": overall["classroom_readiness_stage"],
            "operating_feasibility_score":
                overall["final_operating_feasibility_score"],
            "recommended_action": overall["recommended_action"],
            "recommended_action_label": overall["recommended_action_label"],
            "explanation": overall["explanation"],
            "missing_requirements": overall["missing_requirements"],
            "risk_flags": overall["risk_flags"],
            "next_steps": overall["next_steps"],
            "action_checklist": overall["action_checklist"],
            "signals_only": overall["signals_only"],
            "not_ready_notice": overall["not_ready_notice"],
            # ALLCPR named this ZIP's city a priority expansion market.
            "priority_market": priority_city,
            "is_priority_market": bool(priority_city),
        },
        "courses": courses,
        # Kept for backward compatibility: named + signal leads mixed.
        "top_instructor_leads": top_instructor_leads(
            instructor_candidates, limit=5),
        "top_space_leads": top_space_leads(space_candidates, limit=3),
        # Action-ready split: people/rooms to contact vs where to look.
        "named_instructor_leads": top_instructor_leads(
            named_instructors, limit=5),
        "instructor_signal_leads": instructor_signals[:5],
        "named_space_leads": top_space_leads(named_spaces, limit=5),
        "space_signal_leads": space_signals[:5],
        "lead_counts": {
            "instructors": len(instructor_candidates),
            "spaces": len(space_candidates),
            "named_instructors": len(named_instructors),
            "named_spaces": len(named_spaces),
        },
        # Real competitor pricing + real 6-month student demand for the ZIP
        # (displayed context — pricing is never folded into the score).
        "local_market": market,
        # Dollar-grounded break-even using the real ALLCPR cost structure.
        # Its demand_read also feeds the capped below-break-even penalty in
        # the feasibility score (economics_penalty).
        "unit_economics": economics,
        "last_updated_at": overall["last_updated_at"],
    }
