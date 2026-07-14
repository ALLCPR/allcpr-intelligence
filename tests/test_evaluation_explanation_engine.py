"""Tests for the deterministic explanation engine (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.explanation_engine import (  # noqa: E402
    explain_course_result,
    explain_graph_node,
    explain_recommendation,
    summarize_primary_secondary_avoid,
)
from app.evaluation.score_graph import build_course_score_graph  # noqa: E402
from app.evaluation.score_node import ScoreNode  # noqa: E402


def _strong():
    return build_course_score_graph(
        course_type="arc_cpr", label="ARC CPR",
        historical={"score": 85, "average_students_per_class": 4.68,
                    "total_classes": 120, "fill_rate_percent": 80,
                    "confidence": "high"},
        course_relative={"local_avg": 4.68, "allcpr_avg": 3.5},
        demand={"demand_score": 70, "healthcare_training_ecosystem_score": 65},
        competition={"competition_gap_score": 60},
        schedule={"strength": 65},
        forecast={"expected_students": 4.68, "reference_avg": 3.5,
                  "confidence": "high"},
    )


def _weak():
    return build_course_score_graph(
        course_type="skills_session", label="Skills Session",
        historical={"score": 25, "average_students_per_class": 2.27,
                    "total_classes": 8, "confidence": "low"},
        course_relative={"local_avg": 2.27, "allcpr_avg": 6.0},
        demand={"demand_score": 28},
        competition={"competition_gap_score": 35},
    )


def test_strong_explanation_uses_real_value_and_primary_label():
    text = explain_course_result(_strong())
    assert "ARC CPR" in text
    assert "4.68" in text                 # the real historical average
    assert "PRIMARY" in text.upper()


def test_strong_explanation_does_not_invent_numbers():
    # 2.27 belongs to the weak course; it must never leak into the strong one.
    assert "2.27" not in explain_course_result(_strong())


def test_weak_explanation_warns_and_suggests_validation():
    text = explain_course_result(_weak()).lower()
    assert "2.27" in text
    assert "below" in text
    assert "validation" in text or "test" in text


def test_explain_missing_node_does_not_fabricate_value():
    node = ScoreNode(
        key="forecast_expected_students", label="Forecast expected students",
        value=None, weight=0.0, confidence=0.0, contribution=0.0,
        reasons=["No forecast available."], missing=True,
    )
    text = explain_graph_node(node)
    assert "unknown" in text.lower() or "not available" in text.lower()
    assert "None" not in text


def test_explain_present_node_states_contribution():
    result = _strong()
    hist = next(n for n in result.nodes if n.key == "historical_performance")
    text = explain_graph_node(hist)
    assert "Historical enrollment" in text
    assert f"{hist.contribution:.0f}" in text


def test_explain_recommendation_mentions_action():
    text = explain_recommendation(_strong())
    assert "primary" in text.lower() or "expand" in text.lower()


def test_summary_buckets_primary_secondary_avoid():
    summary = summarize_primary_secondary_avoid([_strong(), _weak()])
    assert "ARC CPR" in summary
    assert "Skills Session" in summary
    # The strong course is primary, the weak one is avoid/test.
    assert "Primary" in summary
