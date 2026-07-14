"""Tests for the modeled-vs-historical backtest."""
from __future__ import annotations

from scripts.backtest_modeled_vs_historical import (
    _correlation_and_fit,
    compute_backtest,
    correlation_for,
)


def test_correlation_for_subset():
    national = [{"zip": "A", "overall": 10}, {"zip": "B", "overall": 20},
                {"zip": "C", "overall": 30}]
    historical = [{"zip": "A", "demand_score": 10}, {"zip": "B", "demand_score": 20},
                  {"zip": "C", "demand_score": 30}]
    # All three -> perfect correlation.
    full = correlation_for(national, historical)
    assert round(full["correlation"], 3) == 1.0
    # Restrict to a subset of two ZIPs.
    sub = correlation_for(national, historical, zips=["A", "B"])
    assert sub["sample_size"] == 2


def test_perfect_correlation():
    pairs = [(0, 0), (1, 2), (2, 4), (3, 6)]
    out = _correlation_and_fit(pairs)
    assert out["sample_size"] == 4
    assert round(out["correlation"], 3) == 1.0
    assert round(out["r2"], 3) == 1.0
    assert round(out["slope"], 3) == 2.0


def test_constant_input_is_safe():
    out = _correlation_and_fit([(5, 1), (5, 2), (5, 3)])   # x constant
    assert out["correlation"] is None
    assert out["r2"] is None


def test_too_few_points():
    out = _correlation_and_fit([(1, 1)])
    assert out["sample_size"] == 1
    assert out["correlation"] is None


def test_compute_backtest_with_overlap():
    national = [
        {"zip": "95112", "overall": 80, "bls_demand": 70, "cpr_demand": 60},
        {"zip": "10016", "overall": 40, "bls_demand": 30, "cpr_demand": 50},
        {"zip": "99999", "overall": 90, "bls_demand": 80, "cpr_demand": 70},
    ]
    historical = [
        {"zip": "95112", "demand_score": 85, "aha_bls_students": 100,
         "arc_bls_students": 50, "arc_cpr_students": 40, "class_count": 30,
         "avg_students": 10, "fill_rate": 80, "total_students": 190},
        {"zip": "10016", "demand_score": 45, "aha_bls_students": 20,
         "arc_bls_students": 10, "arc_cpr_students": 30, "class_count": 5,
         "avg_students": 6, "fill_rate": 60, "total_students": 60},
        {"zip": "70000", "demand_score": 10},   # no modeled match
    ]
    out = compute_backtest(national, historical)
    assert out["sample_size"] == 2            # 95112 + 10016 overlap
    assert "overall_vs_historical_score" in out["metrics"]
    assert out["metrics"]["overall_vs_historical_score"]["sample_size"] == 2
    assert "modeled_overall_vs_proven_demand" in out["metrics"]
    assert out["model_agreement_summary"]
    assert out["modeled_vs_proven"][0]["model_agreement"]
    assert out["top_modeled_zips"][0]["zip"] == "95112"
    assert isinstance(out["notes"], list) and out["notes"]


def test_no_overlap_is_safe():
    out = compute_backtest(
        [{"zip": "11111", "overall": 50, "bls_demand": 40, "cpr_demand": 30}],
        [{"zip": "22222", "demand_score": 60}])
    assert out["sample_size"] == 0
    assert any("No ZIP overlap" in n for n in out["notes"])
    # metrics still present but empty samples.
    assert out["metrics"]["overall_vs_historical_score"]["sample_size"] == 0


def test_false_positive_and_negative_flags():
    national = [
        {"zip": "AAAAA", "overall": 75, "bls_demand": 1, "cpr_demand": 1},  # FP
        {"zip": "BBBBB", "overall": 20, "bls_demand": 1, "cpr_demand": 1},  # FN
    ]
    historical = [
        {"zip": "AAAAA", "demand_score": 10},
        {"zip": "BBBBB", "demand_score": 80},
    ]
    out = compute_backtest(national, historical)
    assert {fp["zip"] for fp in out["false_positives"]} == {"AAAAA"}
    assert {fn["zip"] for fn in out["false_negatives"]} == {"BBBBB"}
