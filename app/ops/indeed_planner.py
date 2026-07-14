"""
Indeed posting planner — the operational bridge for the "source externally"
lane. ALLCPR recruits instructors by posting Indeed jobs (mostly "AHA
Instructor") managed in Manatal ATS, one post per ZIP, three free posts a
month, sponsoring beyond that. This module turns a target ZIP + course into a
staff-ready posting recommendation that respects those exact rules — instead of
a generic search query.

It also ranks *whether* a ZIP is worth posting in, using signals the ops layer
already computes: real local demand vs the break-even it produces, and how thin
instructor supply is. Post where classes can actually be filled.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.ops.models import COURSE_LABELS
from app.ops.recruiting_policy import (
    INDEED_POLICY,
    INSTRUCTOR_SCREENING,
    default_job_title,
)

# Sponsor-approval routing (Indeed handoff SOP). Addresses are internal work
# routing, not secrets, but we surface only what staff need to send the request.
_SPONSOR_TO = "alhe@usjus.org"
_SPONSOR_CC = ("corneliachen@usjus.org", "claudewang@usjus.org",
               "kedimiao@usjus.org", "lynnwang@usjus.org")


def _place(city: str, state: str, zip_code: str) -> str:
    loc = ", ".join(p for p in (city.strip(), state.strip()) if p)
    if loc and zip_code:
        return f"{loc} {zip_code}"
    return loc or zip_code or "[location]"


def recommend_posting(demand_ctx: Optional[Dict[str, Any]],
                      economics: Optional[Dict[str, Any]],
                      instructor_readiness_score: Optional[float]
                      ) -> Dict[str, Any]:
    """Should we post here, and how urgently?

    High when instructor supply is thin AND demand can clear break-even.
    Uses only already-computed ops signals — never re-scrapes.
    """
    reasons = []
    score = 0

    inst = instructor_readiness_score if instructor_readiness_score is not None \
        else 100.0
    if inst < 50:
        score += 2
        reasons.append("instructor supply is thin here")
    elif inst < 80:
        score += 1
        reasons.append("instructor supply is not yet confirmed")

    dr = (economics or {}).get("demand_read") or {}
    pct = dr.get("demand_vs_break_even_pct")
    if pct is not None:
        if pct >= 100:
            score += 2
            reasons.append("local demand already clears break-even")
        elif pct >= 40:
            score += 1
            reasons.append(f"local demand is ~{pct:.0f}% of break-even")
        else:
            reasons.append(f"local demand is only ~{pct:.0f}% of break-even — "
                           "fill classes via enterprise/community first")

    students = (demand_ctx or {}).get("student_count") or 0
    if students >= 20:
        score += 1
        reasons.append(f"{students} real students from this ZIP in 6 months")

    if score >= 3:
        priority = "high"
    elif score >= 1:
        priority = "medium"
    else:
        priority = "low"
    return {"priority": priority, "reasons": reasons}


def build_indeed_plan(course: str, zip_code: str = "", city: str = "",
                      state: str = "", free_posts_used_this_month: int = 0,
                      demand_ctx: Optional[Dict[str, Any]] = None,
                      economics: Optional[Dict[str, Any]] = None,
                      instructor_readiness_score: Optional[float] = None
                      ) -> Dict[str, Any]:
    """A ready-to-execute Indeed posting plan for one ZIP + course."""
    label = COURSE_LABELS.get(course, course)
    job_title = default_job_title(label)
    place = _place(city, state, zip_code)

    free_remaining = max(0, INDEED_POLICY["free_posts_per_month"]
                         - max(0, int(free_posts_used_this_month)))
    use_free = free_remaining > 0
    daily = INDEED_POLICY["sponsor_min_daily_usd"]
    days = INDEED_POLICY["sponsor_round_days"]

    if use_free:
        action = "post_free"
        action_label = (f"Post a free job ({free_remaining} free post(s) left "
                        "this month)")
        sponsor = None
    else:
        action = "sponsor"
        action_label = ("Monthly free-post limit reached — sponsor this post")
        sponsor = {
            "min_daily_usd": daily,
            "round_days": days,
            "estimated_min_cost_usd": round(daily * days, 2),
            "budget_note": ("Sponsored daily spend is driven by exposure and "
                            "can exceed the set budget."),
            "approval_email": {
                "to": _SPONSOR_TO,
                "cc": list(_SPONSOR_CC),
                "subject": f"Indeed Sponsor request — {job_title} — {place}",
                "required_fields": INDEED_POLICY["sponsor_approval"][
                    "required_fields"],
                "report_back_after": INDEED_POLICY["sponsor_approval"][
                    "report_back"],
            },
        }

    eq = INSTRUCTOR_SCREENING["equipment_required"]
    return {
        "course": course,
        "course_label": label,
        "zip": zip_code,
        "job_title": job_title,
        "location_to_set": place,
        "recommendation": recommend_posting(
            demand_ctx, economics, instructor_readiness_score),
        "posting_action": action,
        "posting_action_label": action_label,
        "free_posts_remaining_this_month": free_remaining,
        "sponsor": sponsor,
        "ats": INDEED_POLICY["ats"],
        "repost_cadence": INDEED_POLICY["repost_cadence"],
        "screening_line": (
            f"Require ~{eq['adult_manikins']} sets of manikins "
            f"({eq['adult_manikins']} Adult, {eq['infant_manikins']} Infant, "
            f"{eq['aed_trainers']} AED Trainers; {eq['minimum_acceptable_sets']} "
            "acceptable). No weekend interviews."),
        "rules": INDEED_POLICY["notes"],
    }
