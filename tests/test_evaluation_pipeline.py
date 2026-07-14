"""Tests for the evaluation pipeline orchestrator (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.evaluation_pipeline import build_evaluation_graph  # noqa: E402


def _perf():
    return {
        "area_label": "Milpitas, CA",
        "overall": {"average_students_per_class": 5.0, "total_classes": 46},
        "course_types": [
            {"course_type": "arc_cpr", "label": "ARC CPR",
             "average_students_per_class": 9.0, "total_classes": 40,
             "fill_rate_percent": 80, "course_performance_score": 85},
            {"course_type": "skills_session", "label": "Skills Session",
             "average_students_per_class": 2.2, "total_classes": 6,
             "fill_rate_percent": None, "course_performance_score": 25},
        ],
        "schedule_intelligence": {
            "best_day": {"label": "Saturday", "basis": "enrollment",
                         "average_students_per_class": 8.0, "classes": 10},
        },
        "forecast": {
            "course_types": [
                {"course_type": "arc_cpr", "expected_students": 9.0,
                 "confidence": "high"},
                {"course_type": "skills_session", "expected_students": 2.2,
                 "confidence": "low"},
            ],
        },
    }


def _signals():
    return dict(
        demand={"demand_score": 70, "healthcare_training_ecosystem_score": 65},
        competition={"competition_gap_score": 60},
    )


def test_returns_none_without_course_data():
    assert build_evaluation_graph(None, **_signals()) is None
    assert build_evaluation_graph({"course_types": []}, **_signals()) is None


def test_builds_one_graph_per_course_type():
    graph = build_evaluation_graph(_perf(), **_signals())
    assert graph is not None
    assert len(graph["course_opportunity_graph"]) == 2
    keys = {g["course_type"] for g in graph["course_opportunity_graph"]}
    assert keys == {"arc_cpr", "skills_session"}


def test_strong_course_lands_in_primary_weak_in_avoid():
    graph = build_evaluation_graph(_perf(), **_signals())
    assert "ARC CPR" in graph["primary"]
    assert "Skills Session" in graph["avoid_or_test"]
    assert "ARC CPR" not in graph["avoid_or_test"]


def test_graph_entries_are_json_ready_dicts():
    graph = build_evaluation_graph(_perf(), **_signals())
    entry = graph["course_opportunity_graph"][0]
    assert "final_score" in entry
    assert "nodes" in entry and isinstance(entry["nodes"], list)
    assert "penalty" in entry


def test_summary_and_confidence_notes_present():
    graph = build_evaluation_graph(_perf(), **_signals())
    assert isinstance(graph["summary"], str) and graph["summary"]
    assert isinstance(graph["confidence_notes"], list)
    # The weak, tiny-sample course should generate a low-confidence note.
    joined = " ".join(graph["confidence_notes"]).lower()
    assert "skills session" in joined


def test_missing_forecast_block_still_builds():
    perf = _perf()
    perf.pop("forecast")
    graph = build_evaluation_graph(perf, **_signals())
    arc = next(g for g in graph["course_opportunity_graph"]
               if g["course_type"] == "arc_cpr")
    fnode = next(n for n in arc["nodes"]
                 if n["key"] == "forecast_expected_students")
    assert fnode["missing"] is True
    assert fnode["value"] is None
