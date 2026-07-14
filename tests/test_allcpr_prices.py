"""Tests for the ALLCPR per-state actual-price lookup."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import allcpr_prices  # noqa: E402
from app.scoring.profitability import estimate_profitability  # noqa: E402


_FAKE_TABLE = {
    "CA": {"median_price": 89.0, "sample_size": 102, "overall_median": 79.0},
    "GA": {"median_price": 65.0, "sample_size": 12, "overall_median": 79.0},
    "AR": {"median_price": 79.0, "sample_size": 1, "overall_median": 79.0},
    "XX": {"median_price": 0.0, "sample_size": 5, "overall_median": 79.0},
}


def test_state_with_reliable_sample(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = allcpr_prices.lookup_price("CA")
    assert out.avg_price == 89.0
    assert out.source == "state:CA"
    assert out.sample_size == 102


def test_state_with_small_sample_falls_to_overall(monkeypatch):
    """n<2 falls back to overall median, not the state's single sample."""
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = allcpr_prices.lookup_price("AR")
    assert out.source == "overall_median"
    assert out.avg_price == 79.0


def test_zero_state_median_falls_back(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = allcpr_prices.lookup_price("XX")
    assert out.source == "overall_median"
    assert out.avg_price == 79.0


def test_unknown_state_falls_to_overall(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = allcpr_prices.lookup_price("ZZ")
    assert out.source == "overall_median"


def test_empty_state_falls_to_overall(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = allcpr_prices.lookup_price("")
    assert out.source == "overall_median"


def test_no_data_file_falls_to_config_default(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", {})
    out = allcpr_prices.lookup_price("CA")
    assert out.source == "config_default"
    assert out.sample_size == 0


def test_profitability_uses_state_price(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out_ca = estimate_profitability(
        opportunity_score_0_100=60, demand_score_0_100=60,
        training_score_0_100=60, state="CA",
    )
    out_ga = estimate_profitability(
        opportunity_score_0_100=60, demand_score_0_100=60,
        training_score_0_100=60, state="GA",
    )
    # Same utilization, different prices → CA revenue > GA revenue.
    assert out_ca.avg_course_price == 89.0
    assert out_ga.avg_course_price == 65.0
    assert out_ca.revenue_mid > out_ga.revenue_mid
    assert out_ca.price_source == "state:CA"
    assert out_ga.price_source == "state:GA"


def test_profitability_falls_back_when_state_missing(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = estimate_profitability(
        opportunity_score_0_100=60, demand_score_0_100=60,
        training_score_0_100=60, state="",
    )
    assert out.price_source == "overall_median"
    assert out.avg_course_price == 79.0


def test_profitability_default_state_param_is_backward_compat(monkeypatch):
    """Old callers that don't pass state still work — fall back to overall."""
    monkeypatch.setattr(allcpr_prices, "_CACHE", dict(_FAKE_TABLE))
    out = estimate_profitability(
        opportunity_score_0_100=50, demand_score_0_100=50,
        training_score_0_100=50,
    )
    assert out.avg_course_price == 79.0
