"""
Course-performance scoring + strategy (Phase 4B).

Turns the per-course-type aggregates from
``app.enrichers.course_performance`` into:

  - ``course_performance_score`` (0..100) per course type, anchored so that a
    course performing exactly at the local average scores 50; double the local
    average approaches 100; half scores ~25. When fill rate is known it is
    blended in so a course that fills its seats is rewarded.
  - a Primary / Secondary / Avoid-or-test strategy split.
  - one-line plain-English verdicts (the report's "Best Course Strategy").
  - deterministic scheduling recommendations (weekday vs weekend, low-fill,
    skills-session guidance).
  - a public-demand-vs-actual-enrollment comparison.

Everything is deterministic and never invents data: a course type whose average
enrollment is unknown gets ``course_performance_score = None`` and is excluded
from the Primary/Avoid logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Course types that map to the external demand signals the site pipeline
# already collects. Used by the public-demand-vs-actual comparison.
_BLS_TYPES = ("aha_bls", "arc_bls", "allcpr_bls")
_CPR_TYPES = ("aha_cpr", "arc_cpr", "allcpr_cpr", "cpr_first_aid_blended")

# course_performance_score band cutoffs.
_BAND_STRONG = 65.0
_BAND_WEAK = 45.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _band(score: Optional[float]) -> str:
    if score is None:
        return "Unknown"
    if score >= _BAND_STRONG:
        return "Strong"
    if score >= _BAND_WEAK:
        return "Average"
    return "Weak"


def _score_one(
    avg_students: Optional[float],
    fill_rate: Optional[float],
    reference_avg: Optional[float],
) -> Optional[float]:
    """course_performance_score for one course type. None when unknowable."""
    if avg_students is None or not reference_avg:
        return None
    enroll_component = _clamp(50.0 * (avg_students / reference_avg))
    if fill_rate is not None:
        return round(0.7 * enroll_component + 0.3 * _clamp(fill_rate), 1)
    return round(enroll_component, 1)


def score_course_performance(
    performance: Dict[str, Any],
    allcpr_overall_avg: Optional[float] = None,
) -> Dict[str, Any]:
    """Annotate ``performance`` in place with scores + strategy + scheduling.

    ``allcpr_overall_avg`` is the company-wide average enrollment across every
    record (unfiltered); when supplied each course type also gets a
    ``vs_allcpr_avg`` delta so the report can answer "is this area's demand
    above or below ALLCPR's overall behavior?".
    """
    course_types: List[Dict[str, Any]] = performance.get("course_types") or []
    local_ref = (performance.get("overall") or {}).get("average_students_per_class")

    for ct in course_types:
        avg = ct.get("average_students_per_class")
        fill = ct.get("fill_rate_percent")
        score = _score_one(avg, fill, local_ref)
        ct["course_performance_score"] = score
        ct["performance_band"] = _band(score)
        ct["vs_local_avg"] = (
            round(avg - local_ref, 2)
            if avg is not None and local_ref is not None else None
        )
        ct["vs_allcpr_avg"] = (
            round(avg - allcpr_overall_avg, 2)
            if avg is not None and allcpr_overall_avg is not None else None
        )

    strategy = _build_strategy(course_types)
    scheduling = _scheduling_recommendations(course_types, performance)

    performance["scored"] = True
    performance["local_reference_avg"] = local_ref
    performance["allcpr_overall_avg"] = allcpr_overall_avg
    performance["strategy"] = strategy
    performance["scheduling_recommendations"] = scheduling
    return performance


# --------------------------------------------------------------------------- #
# Strategy: Primary / Secondary / Avoid-or-test
# --------------------------------------------------------------------------- #

def _eligible(ct: Dict[str, Any]) -> bool:
    """A course type is decision-eligible when it is a real, named course type
    with a score and at least 2 classes of evidence. ``unknown_course_type``
    is never a recommendation — it's the un-classifiable bucket."""
    return (ct.get("course_type") != "unknown_course_type"
            and ct.get("course_performance_score") is not None
            and ct.get("total_classes", 0) >= 2)


def _build_strategy(course_types: List[Dict[str, Any]]) -> Dict[str, Any]:
    eligible = [ct for ct in course_types if _eligible(ct)]
    eligible.sort(key=lambda c: c["course_performance_score"], reverse=True)

    primary: List[str] = []
    secondary: List[str] = []
    avoid: List[str] = []
    verdicts: List[str] = []

    for idx, ct in enumerate(eligible):
        label = ct["label"]
        score = ct["course_performance_score"]
        band = ct["performance_band"]
        if idx == 0 and band in ("Strong", "Average"):
            primary.append(label)
            verdicts.append(
                f"{label} appears strongest in this area based on historical "
                f"enrollment ({ct['average_students_per_class']} students/class, "
                f"score {score:.0f}/100)."
            )
        elif band == "Weak":
            avoid.append(label)
            verdicts.append(
                f"{label} demand is below the local average here "
                f"({ct['average_students_per_class']} students/class); do not "
                f"prioritize this course type without paid-search validation."
            )
        else:
            secondary.append(label)

    # If nothing cleared the 'Strong/Average primary' bar, promote the single
    # best eligible course type so the report always has a lead recommendation.
    if not primary and eligible:
        best = eligible[0]
        primary.append(best["label"])
        if best["label"] in secondary:
            secondary.remove(best["label"])

    return {
        "primary": primary,
        "secondary": secondary,
        "avoid_or_test": avoid,
        "verdicts": verdicts,
    }


# --------------------------------------------------------------------------- #
# Scheduling recommendations
# --------------------------------------------------------------------------- #

def _weekday_weekend_totals(
    course_types: List[Dict[str, Any]],
) -> Tuple[List[float], List[float]]:
    weekday_avgs: List[float] = []
    weekend_avgs: List[float] = []
    for ct in course_types:
        ww = ct.get("weekday_vs_weekend") or {}
        wd = (ww.get("weekday") or {}).get("average_students_per_class")
        we = (ww.get("weekend") or {}).get("average_students_per_class")
        if wd is not None:
            weekday_avgs.append(float(wd))
        if we is not None:
            weekend_avgs.append(float(we))
    return weekday_avgs, weekend_avgs


def _scheduling_recommendations(
    course_types: List[Dict[str, Any]],
    performance: Dict[str, Any],
) -> List[str]:
    recs: List[str] = []

    # Weekday vs weekend.
    weekday_avgs, weekend_avgs = _weekday_weekend_totals(course_types)
    if weekday_avgs and weekend_avgs:
        wd = sum(weekday_avgs) / len(weekday_avgs)
        we = sum(weekend_avgs) / len(weekend_avgs)
        if we >= wd * 1.15:
            recs.append(
                f"Weekend classes fill better (avg {we:.1f} vs weekday "
                f"{wd:.1f} students) — schedule more weekend sessions."
            )
        elif wd >= we * 1.15:
            recs.append(
                f"Weekday classes fill better (avg {wd:.1f} vs weekend "
                f"{we:.1f} students) — test additional weekday evening sessions."
            )

    # Low-fill course types.
    for ct in course_types:
        fill = ct.get("fill_rate_percent")
        if fill is not None and fill < 50 and ct.get("total_classes", 0) >= 2:
            recs.append(
                f"Reduce low-fill {ct['label']} classes "
                f"(fill rate {fill:.0f}%)."
            )

    # Skills-session guidance: only schedule when blended CPR/First Aid demand
    # actually exists in the data.
    by_type = {ct["course_type"]: ct for ct in course_types}
    skills = by_type.get("skills_session")
    blended = by_type.get("cpr_first_aid_blended")
    if skills is not None:
        blended_strong = (
            blended is not None
            and (blended.get("course_performance_score") or 0) >= _BAND_WEAK
        )
        if not blended_strong:
            recs.append(
                "Add skills sessions only when blended CPR/First Aid demand is "
                "present — standalone skills demand is thin here."
            )

    if not recs:
        recs.append(
            "Not enough scheduling signal (enrollment, capacity or dates "
            "missing) to recommend day-part changes — collect more class history."
        )
    return recs


# --------------------------------------------------------------------------- #
# Public demand (external signals) vs actual enrollment
# --------------------------------------------------------------------------- #

def _sum_avg_for(course_types: List[Dict[str, Any]], keys: Tuple[str, ...]
                 ) -> Optional[float]:
    avgs = [
        ct["average_students_per_class"] for ct in course_types
        if ct["course_type"] in keys
        and ct.get("average_students_per_class") is not None
    ]
    return round(sum(avgs) / len(avgs), 2) if avgs else None


def compare_public_demand(
    performance: Dict[str, Any],
    demand_counts: Dict[str, int],
) -> Dict[str, Any]:
    """Compare external public-demand signals with actual enrollment.

    ``demand_counts`` is a category->count map (the site pipeline's
    ``counts_5mi``). We translate the BLS-heavy demand drivers (hospitals,
    nursing/medical schools, EMS, fire) into a coarse "public demand leans
    BLS" read and check whether ALLCPR's *actual* BLS enrollment matches.
    """
    course_types = performance.get("course_types") or []

    def c(*keys: str) -> int:
        return sum(int(demand_counts.get(k, 0) or 0) for k in keys)

    bls_demand = c("hospital", "urgent_care", "ems", "fire_station",
                   "nursing_school", "medical_school", "cna_training",
                   "healthcare_training")
    cpr_demand = c("childcare_center", "gym", "community_college",
                   "university", "senior_care")

    bls_actual = _sum_avg_for(course_types, _BLS_TYPES)
    cpr_actual = _sum_avg_for(course_types, _CPR_TYPES)

    notes: List[str] = []
    if bls_demand or cpr_demand:
        leaning = "BLS / healthcare-staff" if bls_demand >= cpr_demand else "CPR / community"
        notes.append(
            f"External signals lean {leaning}: {bls_demand} BLS-driving vs "
            f"{cpr_demand} CPR-driving demand sites nearby."
        )
    if bls_actual is not None and cpr_actual is not None:
        if bls_actual >= cpr_actual and bls_demand >= cpr_demand:
            notes.append(
                f"Actual enrollment agrees — BLS classes average {bls_actual} "
                f"students vs CPR {cpr_actual}. Public demand matches behavior."
            )
        elif bls_actual < cpr_actual and bls_demand >= cpr_demand:
            notes.append(
                f"Mismatch — external demand leans BLS but CPR classes actually "
                f"enroll better here ({cpr_actual} vs {bls_actual}). Validate "
                f"before shifting the schedule toward BLS."
            )
        else:
            notes.append(
                f"BLS classes average {bls_actual} students, CPR {cpr_actual}."
            )
    elif bls_demand or cpr_demand:
        notes.append(
            "No actual enrollment to compare against external demand yet — "
            "treat the public-demand read as unvalidated."
        )

    return {
        "bls_demand_sites": bls_demand,
        "cpr_demand_sites": cpr_demand,
        "bls_actual_avg_students": bls_actual,
        "cpr_actual_avg_students": cpr_actual,
        "notes": notes,
    }
