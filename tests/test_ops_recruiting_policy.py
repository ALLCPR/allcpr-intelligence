"""
Tests for the SOP-derived recruiting/operating policy + Indeed posting planner.

Pins the operational rules the engine now speaks: instructor equipment bar,
Indeed free/sponsor limits, company A–E grades, priority expansion markets,
enrollment targets, and the Indeed posting recommendation.
"""
from __future__ import annotations

from app.ops import indeed_planner as ip
from app.ops import recruiting_policy as rp
from app.ops.models import AHA_BLS, ARC_BLS, scrub_sensitive


# --------------------------------------------------------------------------
# Policy constants
# --------------------------------------------------------------------------
def test_screening_equipment_bar():
    eq = rp.INSTRUCTOR_SCREENING["equipment_required"]
    assert eq["adult_manikins"] == 4
    assert eq["infant_manikins"] == 4
    assert eq["aed_trainers"] == 4
    assert eq["minimum_acceptable_sets"] == 3
    assert "weekend" in rp.INSTRUCTOR_SCREENING["interview_policy"].lower()


def test_cost_policy_split_matches_sop():
    cp = rp.INSTRUCTOR_SCREENING["cost_policy"]
    assert "venue/room fees" in cp["allcpr_covers"]
    assert "travel" in cp["instructor_covers"]
    assert "equipment" in cp["instructor_covers"]


def test_indeed_policy_limits():
    assert rp.INDEED_POLICY["free_posts_per_month"] == 3
    assert rp.INDEED_POLICY["one_post_per_zip"] is True
    assert rp.INDEED_POLICY["sponsor_min_daily_usd"] == 5.0
    assert rp.INDEED_POLICY["sponsor_round_days"] == 7


def test_company_grade_bands():
    assert rp.company_grade(96)["grade"] == "A+"
    assert rp.company_grade(92)["grade"] == "A"
    assert rp.company_grade(85)["grade"] == "B"
    assert rp.company_grade(72)["grade"] == "C"
    assert rp.company_grade(65)["grade"] == "D"
    assert rp.company_grade(40)["grade"] == "E"
    assert rp.company_grade(None)["grade"] == "—"


def test_priority_markets_by_city_and_zip():
    assert rp.is_priority_market("Fremont", "CA") is True
    assert rp.is_priority_market("San Jose", "CA") is True
    assert rp.is_priority_market("Chicago", "IL") is False
    assert rp.priority_market_for_zip("95112") == "San Jose"
    assert rp.priority_market_for_zip("94587") == "Union City"
    assert rp.priority_market_for_zip("10001") is None


def test_enrollment_targets():
    assert rp.ENROLLMENT_TARGETS["site_students_per_week"] == 25
    assert rp.ENROLLMENT_TARGETS["site_students_per_month"] == 108


def test_site_health_checklist_has_eleven_unchecked_items():
    items = rp.site_health_checklist()
    assert len(items) == 11
    assert all(i["done"] is False for i in items)
    keys = {i["key"] for i in items}
    assert "manikin_in_place" in keys and "wifi_normal" in keys


# --------------------------------------------------------------------------
# Indeed planner
# --------------------------------------------------------------------------
def test_indeed_plan_free_when_under_limit():
    plan = ip.build_indeed_plan(AHA_BLS, zip_code="95112", city="San Jose",
                                state="CA", free_posts_used_this_month=1)
    assert plan["job_title"] == "AHA BLS Instructor"
    assert plan["posting_action"] == "post_free"
    assert plan["free_posts_remaining_this_month"] == 2
    assert plan["sponsor"] is None
    assert "San Jose" in plan["location_to_set"]


def test_indeed_plan_sponsor_when_limit_reached():
    plan = ip.build_indeed_plan(ARC_BLS, zip_code="95112",
                                free_posts_used_this_month=3)
    assert plan["posting_action"] == "sponsor"
    sp = plan["sponsor"]
    assert sp["estimated_min_cost_usd"] == 35.0   # $5/day × 7 days
    assert sp["approval_email"]["to"]
    assert "target ZIP code" in sp["approval_email"]["required_fields"]


def test_indeed_recommendation_priority_from_signals():
    # Thin instructor supply + demand clearing break-even → high priority.
    econ = {"demand_read": {"demand_vs_break_even_pct": 130}}
    demand = {"student_count": 40}
    plan = ip.build_indeed_plan(AHA_BLS, zip_code="95112", demand_ctx=demand,
                                economics=econ, instructor_readiness_score=20.0)
    assert plan["recommendation"]["priority"] == "high"
    # Healthy supply + weak demand → low priority.
    plan2 = ip.build_indeed_plan(
        AHA_BLS, zip_code="95112",
        demand_ctx={"student_count": 2},
        economics={"demand_read": {"demand_vs_break_even_pct": 5}},
        instructor_readiness_score=90.0)
    assert plan2["recommendation"]["priority"] in ("low", "medium")


def test_indeed_plan_scrubs_clean():
    plan = ip.build_indeed_plan(AHA_BLS, zip_code="95112",
                                free_posts_used_this_month=3)
    assert "door_code" not in str(scrub_sensitive(plan))
