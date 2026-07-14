"""Tests for proven-demand scoring and model calibration."""
from __future__ import annotations

from app.scoring.historical_proven_demand import (
    classify_historical_confidence,
    compute_course_proven_scores,
    compute_proven_demand_score,
)
from app.scoring.model_calibration import compare_modeled_vs_proven


def test_proven_demand_balances_history_components():
    row = {
        "zip": "95112",
        "class_count": 30,
        "avg_students": 10,
        "fill_rate": 80,
        "arc_cpr_students": 180,
        "arc_bls_students": 60,
        "aha_bls_students": 30,
        "trend": "growing",
    }
    out = compute_proven_demand_score(row)
    assert out["proven_demand_score"] > 70
    assert out["historical_confidence"] == "high"
    assert out["best_historical_course"] == "ARC CPR"
    assert out["historical_course_mix"]["ARC CPR"] > 0.5


def test_total_students_can_be_derived_safely():
    out = compute_proven_demand_score({"class_count": 10, "avg_students": 8})
    assert out["proven_total_students"] == 80
    assert out["proven_demand_score"] is not None


def test_historical_confidence_thresholds():
    assert classify_historical_confidence({"class_count": 4}) == "low"
    assert classify_historical_confidence({"class_count": 5}) == "medium"
    assert classify_historical_confidence({"class_count": 20}) == "high"


def test_course_scores_are_confidence_adjusted():
    low = compute_course_proven_scores(
        {"class_count": 1, "arc_cpr_students": 150})
    high = compute_course_proven_scores(
        {"class_count": 30, "arc_cpr_students": 150})
    assert low["proven_arc_cpr_score"] < high["proven_arc_cpr_score"]


def test_model_calibration_detects_hidden_opportunity():
    modeled = {"zip": "95112", "overall": 55}
    historical = {
        "zip": "95112",
        "class_count": 30,
        "avg_students": 12,
        "fill_rate": 85,
        "total_students": 300,
    }
    out = compare_modeled_vs_proven(modeled, historical)
    assert out["historical_status"] == "has_allcpr_history"
    assert out["model_agreement"] == "hidden_opportunity"
    assert out["model_error"] > 0

