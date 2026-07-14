"""
Performance-based instructor sourcing: "find new instructors who look like our
best ones — and who are actually eligible to teach the course we need."

Two things make this correct rather than a name dump:

1. Eligibility is credential-gated. You cannot teach an AHA BLS class without a
   current AHA BLS Instructor credential aligned to an AHA Training Center, and
   you cannot teach a Red Cross class without a current Red Cross Instructor
   authorization under a Licensed Training Provider. The two are separate
   credentialing systems (Red Cross offers a bridge for already-certified
   instructors; the AHA does not). ``CREDENTIAL_REQUIREMENTS`` encodes this.

2. The target profile is learned from real performance. ``winning_profile``
   summarizes the proven top performers for a course; the plan then finds
   look-alikes three honest ways:
     - ACTIVATE: instructors ALLCPR already has who are proven for this course
       (schedule them more — zero acquisition cost).
     - BRIDGE: proven high performers credentialed in the *other* product line
       who could be cross-trained/bridged to become eligible for this course.
     - SOURCE EXTERNALLY: an ideal-candidate spec + eligibility-aware search
       queries, because external people are not in any dataset we hold.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from app.ops.indeed_planner import build_indeed_plan
from app.ops.instructor_performance import (
    load_instructor_performance,
    top_performers,
)
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED, COURSE_LABELS
from app.ops.recruiting_policy import (
    ALLCPR_CREDENTIAL_ROLE,
    INSTRUCTOR_SCREENING,
)

# --------------------------------------------------------------------------
# Credential eligibility rules (researched, current as of 2026)
# --------------------------------------------------------------------------
_AHA_RULE = {
    "credential": "AHA BLS Instructor",
    "issuing_body": "American Heart Association (AHA)",
    "prerequisites": [
        "Hold a current AHA BLS Provider card",
        "Complete AHA BLS Instructor Essentials (online) + a hands-on "
        "Instructor Course led by AHA Training Faculty",
        "Teach a monitored first course within 6 months of Instructor "
        "Essentials",
        "Align with a primary AHA Training Center (TC) — required to issue "
        "AHA course-completion cards",
    ],
    "validity": "2 years",
    "bridge": ("No AHA bridge exists — a candidate must complete the AHA BLS "
               "Instructor Course. A proven Red Cross/other-org instructor is "
               "still an excellent candidate to put through it."),
    "sources": [
        "https://cpr.heart.org/en/course-formats/find-a-course/"
        "instructor-network",
    ],
}
_ARC_RULE = {
    "credential": "American Red Cross Instructor (BLS / First Aid-CPR-AED)",
    "issuing_body": "American Red Cross (ARC)",
    "prerequisites": [
        "Hold a current Red Cross First Aid/CPR/AED (or higher, e.g. BLS) "
        "certification",
        "Complete the Red Cross Instructor course (online prework + ~8h "
        "in-person)",
        "Affiliate with a Licensed Training Provider (LTP) — required to "
        "report courses and issue certificates",
    ],
    "validity": "2 years",
    "bridge": ("Red Cross offers a free Instructor Bridge for instructors "
               "already certified with another organization (e.g. AHA) — the "
               "fastest way to make a proven AHA instructor ARC-eligible."),
    "sources": [
        "https://www.redcross.org/take-a-class/instructor-training",
    ],
}

CREDENTIAL_REQUIREMENTS: Dict[str, Dict[str, Any]] = {
    AHA_BLS: _AHA_RULE,
    ARC_BLS: _ARC_RULE,
    ARC_CPR_FA_AED: _ARC_RULE,
}

# Which product line a course belongs to, for bridge logic.
_AHA_COURSES = {AHA_BLS}
_ARC_COURSES = {ARC_BLS, ARC_CPR_FA_AED}


def credential_requirement(course: str) -> Dict[str, Any]:
    """The eligibility rule for a course (defaults to the ARC rule)."""
    return CREDENTIAL_REQUIREMENTS.get(course, _ARC_RULE)


def is_eligible(row: Dict[str, Any], course: str) -> bool:
    """True when an instructor has proven (taught) this course already."""
    return course in (row.get("proven_courses") or [])


def can_bridge_to(row: Dict[str, Any], course: str) -> bool:
    """True when a proven instructor is in the *other* product line and could
    be cross-trained/bridged to become eligible for ``course`` (and is not
    already eligible for it).
    """
    if is_eligible(row, course):
        return False
    proven = set(row.get("proven_courses") or [])
    if course in _AHA_COURSES:
        return bool(proven & _ARC_COURSES)   # ARC instructor → bridge to AHA
    return bool(proven & _AHA_COURSES)       # AHA instructor → bridge to ARC


# --------------------------------------------------------------------------
# Winning profile
# --------------------------------------------------------------------------
def winning_profile(rows: List[Dict[str, Any]], course: Optional[str] = None,
                    sample: int = 10) -> Dict[str, Any]:
    """Summarize the proven top performers into a target profile.

    Reports the fill-rate / volume / reach benchmark a new hire should aim for
    and the credential mix of the people who actually hit it.
    """
    top = top_performers(rows, course=course, limit=sample)
    if not top:
        return {"sample_size": 0, "course": course}

    def _median(key: str) -> Optional[float]:
        vals = [r[key] for r in top if r.get(key)]
        return round(statistics.median(vals), 1) if vals else None

    aha = sum(1 for r in top if r["teaches_aha"])
    arc = sum(1 for r in top if r["teaches_arc"])
    both = sum(1 for r in top if r["teaches_aha"] and r["teaches_arc"])
    return {
        "sample_size": len(top),
        "course": course,
        "course_label": COURSE_LABELS.get(course, "All courses")
        if course else "All courses",
        "benchmark_students_per_class": _median("students_per_class"),
        "benchmark_students_6mo": _median("students"),
        "benchmark_zip_reach": _median("zips"),
        "credential_mix": {"aha": aha, "arc": arc, "both": both},
        "example_top_performers": [
            {"name": r["name"], "students": r["students"],
             "students_per_class": r["students_per_class"],
             "proven_courses": r["proven_courses"],
             "performance_score": r["performance_score"]}
            for r in top[:5]
        ],
    }


# --------------------------------------------------------------------------
# Area affinity (lightweight — state + ZIP3 prefix, no heavy geocoding)
# --------------------------------------------------------------------------
def _near_target(row: Dict[str, Any], zip_code: str = "", state: str = ""
                 ) -> bool:
    home_state = (row.get("home_state") or "").upper()
    home_zip = row.get("home_zip") or ""
    if state and home_state and home_state == state.upper():
        return True
    if zip_code and home_zip and home_zip[:3] == str(zip_code)[:3]:
        return True
    return False


def _candidate_view(row: Dict[str, Any], near: bool) -> Dict[str, Any]:
    return {
        "name": row["name"],
        "performance_score": row["performance_score"],
        "performance_tier": row["performance_tier"],
        "company_grade": row.get("company_grade", "—"),
        "students_6mo": row["students"],
        "students_per_class": row["students_per_class"],
        "proven_courses": row["proven_courses"],
        "home_city": row["home_city"],
        "home_state": row["home_state"],
        "home_zip": row["home_zip"],
        "last_taught": row["last_taught"],
        "near_target_area": near,
    }


def _rank(cands: List[Dict[str, Any]], zip_code: str, state: str
          ) -> List[Dict[str, Any]]:
    """Near-target first, then by performance score."""
    scored = [(_near_target(c, zip_code, state), c) for c in cands]
    scored.sort(key=lambda t: (t[0], t[1]["performance_score"]), reverse=True)
    return [_candidate_view(c, near) for near, c in scored]


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------
def build_sourcing_plan(course: str, zip_code: str = "", city: str = "",
                        state: str = "",
                        rows: Optional[List[Dict[str, Any]]] = None,
                        limit: int = 8,
                        free_posts_used_this_month: int = 0,
                        demand_ctx: Optional[Dict[str, Any]] = None,
                        economics: Optional[Dict[str, Any]] = None,
                        instructor_readiness_score: Optional[float] = None
                        ) -> Dict[str, Any]:
    """Everything needed to add an eligible instructor for ``course`` near an
    area: the learned target profile, the eligibility rule, the ALLCPR
    screening bar, and three concrete candidate lanes (activate / bridge /
    source externally — the last including a ready-to-run Indeed posting plan).
    """
    if rows is None:
        rows = load_instructor_performance()
    course = course if course in CREDENTIAL_REQUIREMENTS else AHA_BLS
    rule = credential_requirement(course)
    profile = winning_profile(rows, course=course)

    activate = _rank([r for r in rows if is_eligible(r, course)],
                     zip_code, state)[:limit]
    bridge = _rank([r for r in rows if can_bridge_to(r, course)],
                   zip_code, state)[:limit]

    # Fallback instructor-supply signal for the Indeed recommender when the
    # caller did not pass one: proven instructors near the area = healthier
    # supply, so a post is less urgent.
    if instructor_readiness_score is None:
        near = [c for c in activate if c.get("near_target_area")]
        instructor_readiness_score = (
            80.0 if near else 45.0 if activate else 10.0)

    other_line = ("Red Cross / ARC" if course in _AHA_COURSES
                  else "AHA")
    this_body = rule["issuing_body"]
    ideal = {
        "must_hold": rule["credential"],
        "issuing_body": this_body,
        "match_our_best": (
            f"Aim for our top-performer benchmark: about "
            f"{profile.get('benchmark_students_per_class') or 'n/a'} students "
            f"per class and multi-ZIP reach."),
        "fastest_internal_path": (
            f"Bridge a proven {other_line} instructor into "
            f"{rule['credential']} — see the Bridge list."),
    }

    return {
        "course": course,
        "course_label": COURSE_LABELS.get(course, course),
        "area": {"zip": zip_code, "city": city, "state": state},
        "eligibility": {
            "credential": rule["credential"],
            "issuing_body": rule["issuing_body"],
            "prerequisites": rule["prerequisites"],
            "validity": rule["validity"],
            "bridge": rule["bridge"],
            "sources": rule.get("sources", []),
            # ALLCPR is itself the LTP / Training Site instructors align to.
            "allcpr_role": (ALLCPR_CREDENTIAL_ROLE["aha"] if course in _AHA_COURSES
                            else ALLCPR_CREDENTIAL_ROLE["arc"]),
            "allcpr_role_note": ALLCPR_CREDENTIAL_ROLE["note"],
        },
        "screening": INSTRUCTOR_SCREENING,
        "target_profile": profile,
        "ideal_candidate": ideal,
        "activate_existing": {
            "note": ("Instructors ALLCPR already has who are proven to teach "
                     f"{COURSE_LABELS.get(course, course)} — schedule these "
                     "first (zero acquisition cost)."),
            "candidates": activate,
            "count": len(activate),
        },
        "bridge_candidates": {
            "note": (f"Proven {other_line} instructors who could be "
                     f"cross-trained/bridged into {rule['credential']} to "
                     f"expand {COURSE_LABELS.get(course, course)} capacity."),
            "candidates": bridge,
            "count": len(bridge),
        },
        "external_sourcing": {
            "note": ("External candidates are not in ALLCPR data — post an "
                     "Indeed job (below) and/or use these eligibility-aware "
                     "searches, then record findings as leads."),
            "indeed_plan": build_indeed_plan(
                course, zip_code=zip_code, city=city, state=state,
                free_posts_used_this_month=free_posts_used_this_month,
                demand_ctx=demand_ctx, economics=economics,
                instructor_readiness_score=instructor_readiness_score),
            "queries": eligibility_search_queries(course, zip_code, city,
                                                  state),
        },
    }


def eligibility_search_queries(course: str, zip_code: str = "", city: str = "",
                               state: str = "") -> List[Dict[str, str]]:
    """Credential-correct external search queries for a course + area."""
    place = " ".join(p for p in (city, state) if p).strip() or zip_code or ""
    place_q = f'"{place}"' if place else ""
    out: List[Dict[str, str]] = []

    def add(label: str, query: str) -> None:
        out.append({"label": label, "query": query.strip()})

    if course in _AHA_COURSES:
        add("AHA BLS Instructors nearby",
            f'"AHA BLS Instructor" {place_q}')
        add("AHA Training Center instructors",
            f'"AHA Training Center" instructor {place_q}')
        add("BLS Provider card holders (bridge to instructor)",
            f'"BLS Instructor" OR "BLS Provider" CPR {place_q}')
        add("Hospital/EMS educators (AHA-aligned)",
            f'("clinical educator" OR "EMS educator") BLS {place_q}')
    else:
        add("Red Cross Instructors nearby",
            f'"Red Cross Instructor" (CPR OR BLS) {place_q}')
        add("Red Cross Licensed Training Providers",
            f'"Licensed Training Provider" Red Cross CPR {place_q}')
        add("First Aid/CPR/AED instructors",
            f'"CPR Instructor" OR "First Aid Instructor" {place_q}')
        add("AHA instructors to bridge to Red Cross",
            f'"CPR Instructor" AHA {place_q}')
    # Shared lanes useful for either product line.
    add("Existing CPR training businesses (owners/instructors)",
        f'"CPR training" (instructor OR owner) {place_q}')
    add("LinkedIn CPR/BLS instructors",
        f'site:linkedin.com/in ("BLS Instructor" OR "CPR Instructor") {place_q}')
    return out
