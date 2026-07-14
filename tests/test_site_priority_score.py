"""Tests for the v2.1 site-priority scoring layer."""
from __future__ import annotations

import math

from app.scoring.site_priority_score import (
    SCORE_FORMULA_VERSION,
    annotate_site_priority_scores,
    build_course_priority_profiles,
    build_site_priority_breakdown,
    calculate_competition_profile,
    calculate_final_site_priority_score,
    readable_competition_risk,
)


def _strong_public_row():
    return {
        "zip": "94086",
        "overall": 74.0,
        "bls_demand": 72.0,
        "cpr_demand": 76.0,
        "population": 52_000,
        "population_density": 7_500,
        "median_income": 125_000,
        "working_age_share": 0.74,
        "employment_rate": 0.70,
        "bachelors_or_higher_share": 0.48,
        "healthcare_employment_share": 0.10,
        "healthcare_facility_count": 45,
        "healthcare_facility_density": 10.0,
        "training_school_count": 18,
        "training_school_density": 4.0,
        "hospital_count": 4,
        "urgent_care_count": 5,
        "competitor_count": 4,
        "avg_competitor_rating": 4.4,
        "data_confidence": "ok",
        "enrichment_updated_at": "2026-06-26T00:00:00+00:00",
        "commercial": {
            "commercial_validated": True,
            "commercial_space_count": 2,
            "available_space_count": 1,
            "rent_avg": 3200,
            "parking_summary": "Yes",
            "classroom_fit_summary": "Good",
            "commercial_sources": ["manual"],
            "commercial_updated_at": "2026-06-20",
            "commercial_ready": True,
        },
    }


def test_high_demand_validation_and_commercial_confirmed_gets_strong_label():
    out = calculate_final_site_priority_score(_strong_public_row())
    assert out["score_formula_version"] == SCORE_FORMULA_VERSION
    assert out["market_demand_score"] >= 70
    assert out["validation_evidence_score"] >= 70
    assert out["commercial_feasibility_confirmed"] is True
    assert out["final_site_priority_score"] >= 70
    assert out["site_priority_decision"] == "Ready for site screening"


def test_high_validation_with_missing_commercial_is_capped():
    row = _strong_public_row()
    row.pop("commercial")
    row.pop("commercial_space_available", None)
    out = calculate_final_site_priority_score(row)
    assert out["commercial_feasibility_score"] is None
    assert out["site_priority_score_status"] == "provisional"
    assert out["commercial_feasibility_confirmed"] is False
    assert "validate commercial" in out["site_priority_decision"].lower()
    assert "site_priority_decision_capped" in out["site_priority_risk_flags"]


def test_high_competitors_add_validation_and_saturation_penalty():
    profile = calculate_competition_profile({
        "competitor_count": 23,
        "avg_competitor_rating": 4.85,
    })
    assert profile["competitor_market_validation_score"] >= 90
    assert profile["competition_saturation_penalty"] >= 14
    assert profile["competition_risk_level"] == "saturated_unless_differentiated"


def test_zero_competitors_are_unproven_not_saturated():
    profile = calculate_competition_profile({"competitor_count": 0})
    assert profile["competitor_market_validation_score"] < 40
    assert profile["competition_saturation_penalty"] == 0
    assert profile["competition_risk_level"] == "unproven_market"


def test_places_only_strength_does_not_create_top_tier():
    row = {
        "zip": "99999",
        "overall": 28.0,
        "bls_demand": 25.0,
        "cpr_demand": 31.0,
        "population": 4_000,
        "population_density": 300,
        "median_income": 42_000,
        "healthcare_employment_share": 0.006,
        "healthcare_facility_count": 95,
        "healthcare_facility_density": 60,
        "training_school_count": 80,
        "training_school_density": 40,
        "hospital_count": 20,
        "urgent_care_count": 20,
        "competitor_count": 2,
        "data_confidence": "ok",
    }
    out = calculate_final_site_priority_score(row)
    assert out["validation_evidence_score"] >= 70
    assert out["market_demand_score"] < 55
    assert out["final_site_priority_score"] < 60
    assert out["site_priority_decision"] in {"Watchlist / monitor", "Low priority"}
    assert "places_signal_not_enough_without_public_demand" in out["site_priority_risk_flags"]


def test_95112_style_profile_has_high_validation_and_saturation_risk():
    row = {
        "zip": "95112",
        "overall": 61.9,
        "bls_demand": 50.5,
        "cpr_demand": 73.3,
        "validation_score": 83.9,
        "population": 57_373,
        "population_density": 7_892,
        "median_income": 89_103,
        "healthcare_employment_share": 0.011,
        "healthcare_facility_count": 87,
        "healthcare_facility_density": 11.9674,
        "training_school_count": 60,
        "training_school_density": 8.2534,
        "hospital_count": 20,
        "urgent_care_count": 20,
        "nursing_school_count": 20,
        "competitor_count": 23,
        "avg_competitor_rating": 4.85,
        "commercial_space_available": True,
        "estimated_rent": 2900,
        "rent_source": "manual_commercial_validation_csv",
        "data_confidence": "ok",
        "enrichment_updated_at": "2026-06-26T00:24:55+00:00",
    }
    out = calculate_final_site_priority_score(row)
    assert out["validation_evidence_score"] >= 80
    assert out["competition_risk_level"] == "saturated_unless_differentiated"
    assert out["commercial_feasibility_status"] == "partial"
    assert out["site_priority_decision"] == (
        "Validation-supported opportunity - needs commercial validation"
    )
    assert "saturation risk is high" in out["site_priority_explanation"]


def test_missing_fields_do_not_crash_and_scores_are_bounded():
    out = calculate_final_site_priority_score({"zip": "00000"})
    assert out["site_priority_decision"] == "Insufficient data"
    for key, value in out.items():
        if key.endswith("_score") or key.endswith("_score_used"):
            if value is None:
                continue
            assert isinstance(value, (int, float))
            assert math.isfinite(value)
            assert 0 <= value <= 100


def test_annotator_preserves_old_fields_and_is_deterministic():
    row = _strong_public_row()
    first = annotate_site_priority_scores(row)
    second = annotate_site_priority_scores(row)
    assert first == second
    assert first["overall"] == row["overall"]
    assert first["score_formula_version"] == SCORE_FORMULA_VERSION
    assert "final_site_priority_score" in first


def test_course_profiles_change_selected_course_interpretation():
    row = _strong_public_row()
    row.update({
        "bls_demand": 52.0,
        "cpr_demand": 82.0,
        "healthcare_employment_share": 0.012,
        "community_facility_count": 34,
        "school_count": 18,
        "childcare_count": 9,
        "community_facility_density": 8.5,
        "competitor_count": 23,
        "commercial": {},
        "historical_course_mix": {
            "aha_bls": 0.10,
            "arc_bls": 0.20,
            "arc_cpr": 0.70,
        },
        "best_historical_course": "ARC CPR",
    })
    scores = calculate_final_site_priority_score(row)
    profiles = build_course_priority_profiles(row, scores)

    arc_cpr = profiles["arc_cpr"]
    aha_bls = profiles["aha_bls"]
    arc_bls = profiles["arc_bls"]
    overall = profiles["overall"]

    assert arc_cpr["selected_course_label"] == "ARC CPR"
    assert "Community CPR demand" in " ".join(arc_cpr["course_why_bullets"])
    assert "ARC CPR" in arc_cpr["course_fit_label"]
    assert "differentiation" in arc_cpr["course_specific_next_action"].lower()

    assert aha_bls["selected_course_label"] == "AHA BLS"
    assert "Healthcare/BLS workforce" in " ".join(aha_bls["course_why_bullets"])
    assert aha_bls["course_fit_reason"] != arc_cpr["course_fit_reason"]

    assert arc_bls["selected_course_label"] == "ARC BLS"
    assert "Healthcare and workplace" in " ".join(arc_bls["course_why_bullets"])

    assert "blended market view" in " ".join(overall["course_why_bullets"]).lower()
    assert overall["course_fit_reason"] != arc_cpr["course_fit_reason"]


def test_readable_competition_risk_hides_internal_codes():
    assert readable_competition_risk(
        "saturated_unless_differentiated"
    ) == "Saturated unless differentiated"


def test_score_breakdown_explains_94541_gap():
    """The 94.7-validation vs 54.3-final gap must be spelled out numerically."""
    scores = {
        "market_demand_score": 70.2,
        "validation_evidence_score": 94.7,
        "commercial_feasibility_score_used": 50.0,
        "commercial_feasibility_score": None,
        "commercial_feasibility_status": "unknown",
        "historical_allcpr_fit_score": None,
        "historical_allcpr_fit_score_used": 50.0,
        "competition_saturation_penalty": 16.0,
        "final_site_priority_score": 54.3,
        "site_priority_score_status": "provisional",
        "competition_risk_level": "saturated_unless_differentiated",
    }
    breakdown = build_site_priority_breakdown(scores)

    by_key = {c["key"]: c for c in breakdown["components"]}
    assert by_key["market_demand"]["weight_pct"] == 45
    assert by_key["validation_evidence"]["weight_pct"] == 25
    assert by_key["commercial_feasibility"]["weight_pct"] == 20
    assert by_key["historical_fit"]["weight_pct"] == 10
    # Neutral placeholders must be labelled as such, not shown as real data.
    assert "neutral 50" in by_key["commercial_feasibility"]["note"].lower()
    assert "no allcpr history" in by_key["historical_fit"]["note"].lower()

    # Components minus the penalty must reconstruct the final score.
    contribution = sum(c["weighted_points"] for c in breakdown["components"])
    assert math.isclose(contribution, breakdown["subtotal"], abs_tol=0.05)
    reconstructed = round(
        breakdown["subtotal"] - breakdown["competition_saturation_penalty"], 1
    )
    assert reconstructed == breakdown["final_site_priority_score"] == 54.3
    assert breakdown["capped"] is True

    # Plain-language summary must convey "high validation != ready to open".
    summary = breakdown["summary"].lower()
    assert "validation evidence" in summary
    assert "not that it is ready to open" in summary
    assert "competition saturation subtracts 16" in summary


def test_score_breakdown_is_wired_into_final_scores():
    row = _strong_public_row()
    scores = calculate_final_site_priority_score(row)
    breakdown = scores["site_priority_score_breakdown"]
    assert {c["key"] for c in breakdown["components"]} == {
        "market_demand", "validation_evidence",
        "commercial_feasibility", "historical_fit",
    }
    if scores["final_site_priority_score"] is not None:
        reconstructed = round(
            breakdown["subtotal"] - breakdown["competition_saturation_penalty"], 1
        )
        assert reconstructed == scores["final_site_priority_score"]
