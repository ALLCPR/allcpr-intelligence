"""Tests for the course opportunity score graph (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.course_recommendation import AVOID, EXPAND  # noqa: E402
from app.evaluation.score_graph import (  # noqa: E402
    ScoreGraphResult,
    build_course_score_graph,
)


def _strong_kwargs():
    return dict(
        course_type="arc_cpr",
        label="ARC CPR",
        historical={"score": 85, "average_students_per_class": 9.0,
                    "total_classes": 120, "fill_rate_percent": 80,
                    "confidence": "high"},
        course_relative={"local_avg": 9.0, "allcpr_avg": 6.0},
        demand={"demand_score": 70, "healthcare_training_ecosystem_score": 65},
        competition={"competition_gap_score": 60},
        schedule={"strength": 65},
        forecast={"expected_students": 9.0, "reference_avg": 6.0,
                  "confidence": "high"},
    )


def _weak_kwargs():
    return dict(
        course_type="skills_session",
        label="Skills Session",
        historical={"score": 25, "average_students_per_class": 2.2,
                    "total_classes": 8, "confidence": "low"},
        course_relative={"local_avg": 2.2, "allcpr_avg": 6.0},
        demand={"demand_score": 30},
        competition={"competition_gap_score": 35},
        schedule=None,
        forecast=None,
    )


def test_strong_course_becomes_expand():
    result = build_course_score_graph(**_strong_kwargs())
    assert isinstance(result, ScoreGraphResult)
    assert result.final_score >= 70
    assert result.recommendation.action == EXPAND
    assert result.recommendation.display_group == "Primary"


def test_weak_course_becomes_avoid_or_test():
    result = build_course_score_graph(**_weak_kwargs())
    assert result.final_score < 50
    assert result.recommendation.action in (AVOID, "TEST_ONLY")


def test_contributions_minus_penalty_equal_final_score():
    result = build_course_score_graph(**_strong_kwargs())
    present = [n for n in result.nodes if not n.missing]
    total = sum(n.contribution for n in present)
    # final = round(sum(contributions) - penalty), clamped to 0..100
    assert abs(total - result.penalty.penalty_points - result.final_score) <= 1.0


def test_present_node_weights_renormalize_to_one():
    result = build_course_score_graph(**_weak_kwargs())  # schedule+forecast missing
    present = [n for n in result.nodes if not n.missing]
    assert abs(sum(n.weight for n in present) - 1.0) < 1e-6


def test_missing_forecast_is_not_fabricated():
    result = build_course_score_graph(**_weak_kwargs())
    forecast_nodes = [n for n in result.nodes if n.key == "forecast_expected_students"]
    assert forecast_nodes, "forecast node should still appear in the graph"
    assert forecast_nodes[0].missing is True
    assert forecast_nodes[0].value is None


def test_missing_all_history_lowers_score_and_confidence():
    base = build_course_score_graph(**_strong_kwargs())
    no_hist = dict(_strong_kwargs())
    no_hist["historical"] = None
    no_hist["course_relative"] = None
    result = build_course_score_graph(**no_hist)
    assert result.penalty.penalty_points > base.penalty.penalty_points
    assert result.final_score < base.final_score
    assert result.confidence < base.confidence


def test_node_weights_and_confidence_in_bounds():
    result = build_course_score_graph(**_strong_kwargs())
    for n in result.nodes:
        assert 0.0 <= n.weight <= 1.0
        assert 0.0 <= n.confidence <= 1.0


def test_to_dict_is_json_ready_and_complete():
    result = build_course_score_graph(**_strong_kwargs())
    d = result.to_dict()
    assert d["course_type"] == "arc_cpr"
    assert d["recommendation"] == EXPAND
    assert d["display_group"] == "Primary"
    assert isinstance(d["nodes"], list) and d["nodes"]
    assert "penalty" in d and "penalty_points" in d["penalty"]
    assert 0 <= d["final_score"] <= 100
