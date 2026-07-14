"""Tests for the backtest simulator placeholder (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.backtest_simulator import (  # noqa: E402
    BacktestScenario,
    evaluate_prediction,
)


def test_actual_beats_threshold_is_success():
    assert evaluate_prediction(75.0, actual_students=10, success_threshold=6) is True


def test_actual_below_threshold_is_failure():
    assert evaluate_prediction(75.0, actual_students=3, success_threshold=6) is False


def test_unknown_actual_is_unevaluable():
    # No real outcome yet — we must not pretend it succeeded or failed.
    assert evaluate_prediction(75.0, actual_students=None, success_threshold=6) is None


def test_scenario_computes_success_on_construction():
    s = BacktestScenario(
        area="Milpitas, CA", course_type="arc_cpr",
        predicted_score=72.0, actual_students=9, success_threshold=6,
    )
    assert s.success is True


def test_scenario_success_none_when_actual_unknown():
    s = BacktestScenario(
        area="Milpitas, CA", course_type="arc_cpr",
        predicted_score=72.0, actual_students=None, success_threshold=6,
    )
    assert s.success is None


def test_scenario_to_dict_round_trips():
    s = BacktestScenario(
        area="Milpitas, CA", course_type="arc_cpr",
        predicted_score=72.0, actual_students=4, success_threshold=6,
    )
    d = s.to_dict()
    assert d["area"] == "Milpitas, CA"
    assert d["predicted_score"] == 72.0
    assert d["success"] is False
