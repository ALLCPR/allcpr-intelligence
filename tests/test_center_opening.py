"""Tests for the center-opening decision summary."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.center_opening import (  # noqa: E402
    AVOID,
    DECISIONS,
    OPEN,
    TEST,
    WATCH,
    build_center_opening_recommendations,
    decide,
    write_recommendations_csv,
    write_recommendations_json,
)
from app.evaluation.evaluation_pipeline import build_evaluation_graph  # noqa: E402


# --- decision mapping --------------------------------------------------------

def test_high_score_high_confidence_is_open():
    assert decide(86, "high") == OPEN
    assert decide(80, "medium") == OPEN


def test_high_score_low_confidence_is_test_first():
    assert decide(86, "low") == TEST


def test_medium_score_is_test_or_watch():
    assert decide(70, "high") == TEST
    assert decide(70, "low") == WATCH
    assert decide(55, "medium") == WATCH
    assert decide(55, "low") == AVOID


def test_low_score_is_avoid():
    assert decide(40, "high") == AVOID
    assert decide(40, "low") == AVOID  # cannot go below the floor
    assert decide(82, "very_low") == WATCH  # very_low downgrades two levels


# --- payload building ---------------------------------------------------------

def _perf_with_graph():
    perf = {
        "area_label": "San Jose, CA",
        "area_is_filtered": True,
        "overall": {"average_students_per_class": 5.0, "total_classes": 46},
        "course_types": [
            {"course_type": "arc_cpr", "label": "ARC CPR",
             "average_students_per_class": 9.0, "total_classes": 40,
             "fill_rate_percent": 80, "course_performance_score": 85},
            {"course_type": "skills_session", "label": "Skills Session",
             "average_students_per_class": 2.2, "total_classes": 6,
             "course_performance_score": 25},
        ],
        "schedule_intelligence": {
            "best_day": {"label": "Saturday", "basis": "enrollment",
                         "average_students_per_class": 8.0, "classes": 10},
        },
    }
    perf["evaluation_graph"] = build_evaluation_graph(
        perf,
        demand={"demand_score": 70, "healthcare_training_ecosystem_score": 65},
        competition={"competition_gap_score": 60},
    )
    return perf


def test_every_recommendation_is_complete():
    out = build_center_opening_recommendations(
        _perf_with_graph(), location="Gosvea / University area")
    assert out["n"] == 2
    assert "not a guaranteed prediction" in out["honesty_note"]
    for rec in out["recommendations"]:
        assert rec["city"] == "San Jose, CA"
        assert rec["location"] == "Gosvea / University area"
        assert rec["decision"] in DECISIONS
        assert isinstance(rec["opportunity_score"], float)
        assert rec["confidence"]
        assert rec["reasons"], "reasons must never be empty"
        assert rec["risks"], "risks must never be empty"
        assert rec["next_action"]
        # The future-uncertainty caveat is always one of the risks.
        assert any("ads" in r and "competition" in r for r in rec["risks"])


def test_low_confidence_course_is_never_open():
    out = build_center_opening_recommendations(_perf_with_graph())
    weak = next(r for r in out["recommendations"]
                if r["course_type"] == "skills_session")
    assert weak["confidence"] in ("low", "very_low")
    assert weak["decision"] != OPEN


def test_no_graph_yields_warning_not_fabrication():
    out = build_center_opening_recommendations({"area_label": "Nowhere, CA"})
    assert out["n"] == 0
    assert out["recommendations"] == []
    assert "No scored course history" in out["warning"]


def test_unscored_course_is_skipped():
    perf = _perf_with_graph()
    perf["evaluation_graph"]["course_opportunity_graph"].append(
        {"course_type": "mystery", "label": "Mystery", "final_score": None,
         "penalty": {"confidence_level": "low", "reasons": []}, "nodes": []})
    out = build_center_opening_recommendations(perf)
    assert all(r["course_type"] != "mystery" for r in out["recommendations"])


# --- writers -------------------------------------------------------------------

def test_json_and_csv_outputs(tmp_path):
    out = build_center_opening_recommendations(_perf_with_graph())
    jpath = tmp_path / "recs.json"
    cpath = tmp_path / "recs.csv"
    write_recommendations_json(out, jpath)
    write_recommendations_csv(out, cpath)

    loaded = json.loads(jpath.read_text(encoding="utf-8"))
    assert loaded["n"] == 2
    assert loaded["recommendations"][0]["decision"] in DECISIONS

    with cpath.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["decision"] in DECISIONS
    assert rows[0]["reasons"]  # joined, non-empty
