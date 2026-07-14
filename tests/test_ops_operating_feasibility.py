"""Operating feasibility: formulas + recommended-action decision ladder."""
from __future__ import annotations

import pytest

from app.ops import operating_feasibility as feas
from app.ops.models import AHA_BLS, OVERALL
from tests.ops_fixtures import (
    STRONG_ZIP_ROW,
    WEAK_ZIP_ROW,
    confirmed_instructor,
    confirmed_room,
    named_instructor_lead,
    signal_instructor_lead,
    signal_room,
)


# --------------------------------------------------------------------------
# Formula weights
# --------------------------------------------------------------------------
def test_overall_formula_weights():
    # demand 80, instructor 80, classroom 80, proof unknown (neutral 50):
    # 0.40*80 + 0.25*80 + 0.25*80 + 0.10*50 = 77.0
    score = feas._weighted_feasibility(OVERALL, 80.0, 80.0, 80.0, None, 0.0)
    assert score == 77.0


def test_aha_formula_weights_instructor_heavier():
    # 0.35*80 + 0.40*80 + 0.15*80 + 0.10*50 = 77.0 at equal legs, but a weak
    # instructor leg must hurt AHA more than overall.
    aha_weak_inst = feas._weighted_feasibility(AHA_BLS, 80.0, 20.0, 80.0,
                                               None, 0.0)
    overall_weak_inst = feas._weighted_feasibility(OVERALL, 80.0, 20.0, 80.0,
                                                   None, 0.0)
    assert aha_weak_inst < overall_weak_inst


def test_penalties_subtract():
    base = feas._weighted_feasibility(OVERALL, 80.0, 80.0, 80.0, None, 0.0)
    penalized = feas._weighted_feasibility(OVERALL, 80.0, 80.0, 80.0, None,
                                           10.0)
    assert penalized == base - 10.0


def test_unknown_demand_yields_no_score():
    assert feas._weighted_feasibility(OVERALL, None, 80.0, 80.0, None,
                                      0.0) is None


# --------------------------------------------------------------------------
# Recommended-action fixture scenarios (from the operations spec)
# --------------------------------------------------------------------------
def test_strong_zip_no_instructor_path_at_all():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [], [confirmed_room()])
    assert rec["recommended_action"] == "NOT_READY_NO_INSTRUCTOR"


def test_strong_zip_only_instructor_signals_needs_outreach():
    signals = [signal_instructor_lead(),
               signal_instructor_lead(name="Hospital educators")]
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, signals, [confirmed_room()])
    assert rec["recommended_action"] == "INSTRUCTOR_OUTREACH_NEEDED"
    assert any("instructor" in m.lower()
               for m in rec["missing_requirements"])


def test_strong_zip_instructor_but_no_room():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [])
    assert rec["recommended_action"] == "NOT_READY_NO_SPACE"


def test_strong_zip_instructor_but_weak_room_supply():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [signal_room()])
    assert rec["recommended_action"] == "SPACE_OUTREACH_NEEDED"


def test_strong_zip_confirmed_instructor_and_room_is_test_class_ready():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()])
    assert rec["recommended_action"] == "TEST_CLASS_READY"
    assert rec["final_operating_feasibility_score"] is not None
    assert rec["next_steps"]


def test_weak_demand_beats_strong_supply():
    rec = feas.compute_course_readiness(
        "99999", WEAK_ZIP_ROW, [confirmed_instructor()], [confirmed_room()])
    assert rec["recommended_action"] == "NOT_READY_DEMAND_WEAK"


def test_aha_with_weak_aha_instructor_readiness_is_not_ready():
    # Confirmed ARC-only instructor: ARC may be test-class ready, AHA not.
    arc_only = confirmed_instructor(courses=["ARC_BLS", "ARC_CPR_FA_AED"])
    aha = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [arc_only], [confirmed_room()],
        course=AHA_BLS)
    assert aha["recommended_action"] != "TEST_CLASS_READY"
    assert aha["recommended_action"] == "NOT_READY_NO_INSTRUCTOR"


def test_unmodeled_zip_needs_research():
    rec = feas.compute_course_readiness(
        "00000", {}, [confirmed_instructor()], [confirmed_room()])
    assert rec["recommended_action"] == "RESEARCH_NEEDED"


# --------------------------------------------------------------------------
# SOP test-class threshold + permanent-center gating
# --------------------------------------------------------------------------
def test_sop_test_class_pass_makes_recurring_candidate():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()],
        test_class={"best_week_signups": 6, "total_signups": 6})
    assert rec["recommended_action"] == "RECURRING_CLASS_CANDIDATE"


def test_sop_test_class_total_threshold():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()],
        test_class={"best_week_signups": 3, "total_signups": 10})
    assert rec["recommended_action"] == "RECURRING_CLASS_CANDIDATE"


def test_sop_test_class_below_threshold_stays_test_ready():
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()],
        test_class={"best_week_signups": 2, "total_signups": 4})
    assert rec["recommended_action"] == "TEST_CLASS_READY"


def test_permanent_center_requires_85_band_and_no_risk_flags():
    row = dict(STRONG_ZIP_ROW)
    row.update({
        "market_demand_score": 95.0,
        "aha_bls_score": 95.0, "arc_bls_score": 95.0, "arc_cpr_score": 95.0,
        "bls_demand": 95.0, "cpr_demand": 95.0,
        "historical_status": "has_allcpr_history",
        "proven_demand_score": 90.0,
    })
    rec = feas.compute_course_readiness(
        "95112", row, [confirmed_instructor()], [confirmed_room()],
        test_class={"best_week_signups": 8, "total_signups": 15})
    assert rec["final_operating_feasibility_score"] >= 85.0
    assert rec["recommended_action"] == "PERMANENT_CENTER_CANDIDATE"


def test_saturated_competition_blocks_permanent_center():
    row = dict(STRONG_ZIP_ROW)
    row.update({
        "market_demand_score": 95.0, "bls_demand": 95.0, "cpr_demand": 95.0,
        "aha_bls_score": 95.0, "arc_bls_score": 95.0, "arc_cpr_score": 95.0,
        "historical_status": "has_allcpr_history",
        "proven_demand_score": 90.0,
        "competition_risk_level": "saturated_unless_differentiated",
    })
    rec = feas.compute_course_readiness(
        "95112", row, [confirmed_instructor()], [confirmed_room()],
        test_class={"best_week_signups": 8, "total_signups": 15})
    assert rec["recommended_action"] == "RECURRING_CLASS_CANDIDATE"
    assert any("saturated" in f for f in rec["risk_flags"])


# --------------------------------------------------------------------------
# Output shape
# --------------------------------------------------------------------------
@pytest.mark.parametrize("field", [
    "zip", "course_type", "demand_score", "instructor_readiness_score",
    "aha_instructor_readiness_score", "arc_instructor_readiness_score",
    "classroom_readiness_score", "final_operating_feasibility_score",
    "recommended_action", "recommended_action_label", "missing_requirements",
    "risk_flags", "explanation", "next_steps", "last_updated_at",
    "demand_label", "instructor_readiness", "classroom_readiness",
])
def test_course_readiness_record_shape(field):
    rec = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [named_instructor_lead()], [signal_room()])
    assert field in rec


def test_build_zip_operating_readiness_bundle():
    bundle = feas.build_zip_operating_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()])
    assert bundle["zip"] == "95112"
    assert set(bundle["courses"].keys()) == {
        "OVERALL", "AHA_BLS", "ARC_BLS", "ARC_CPR_FA_AED"}
    summary = bundle["summary"]
    assert summary["recommended_action"] == "TEST_CLASS_READY"
    assert summary["demand_label"] in ("Strong", "Medium", "Weak", "No Signal")
    assert bundle["top_instructor_leads"]
    assert bundle["top_space_leads"]


# -- break-even economics penalty ------------------------------------------
def test_economics_penalty_unknown_data_is_neutral():
    assert feas.economics_penalty(None) == (0.0, None)
    assert feas.economics_penalty({"demand_read": None}) == (0.0, None)


def test_economics_penalty_full_coverage_is_zero():
    econ = {"demand_read": {"demand_vs_break_even_pct": 120.0,
                            "local_students_per_month": 45,
                            "easiest_break_even_students_per_month": 37}}
    assert feas.economics_penalty(econ) == (0.0, None)


def test_economics_penalty_scales_with_shortfall():
    econ = {"demand_read": {"demand_vs_break_even_pct": 11.0,
                            "local_students_per_month": 4,
                            "easiest_break_even_students_per_month": 37}}
    pen, note = feas.economics_penalty(econ)
    assert pen == pytest.approx((1 - 0.11) * feas.ECON_MAX_PENALTY, abs=0.1)
    assert "11% of break-even" in note and "revenue risk" in note


def test_economics_penalty_lowers_feasibility_and_flags_risk():
    econ = {"demand_read": {"demand_vs_break_even_pct": 11.0,
                            "local_students_per_month": 4,
                            "easiest_break_even_students_per_month": 37}}
    base = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()])
    hit = feas.compute_course_readiness(
        "95112", STRONG_ZIP_ROW, [confirmed_instructor()], [confirmed_room()],
        economics=econ)
    assert hit["final_operating_feasibility_score"] < \
        base["final_operating_feasibility_score"]
    assert hit["penalties"]["below_break_even_penalty"] > 0
    assert any("break-even" in f for f in hit["risk_flags"])
