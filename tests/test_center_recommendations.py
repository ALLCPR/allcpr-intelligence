"""Tests for business-facing center-opening recommendation output."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.evaluation.center_recommendations import (
    OPEN,
    TEST,
    build_center_recommendations_from_report,
    decide_center_opening,
    write_recommendations_csv,
    write_recommendations_json,
)


def _candidate(score=59.7, confidence=85.14, validated=False, demand=90.0):
    return {
        "rank": 1,
        "profile": {
            "city": "San Jose",
            "state": "CA",
            "candidate_name": "Needs commercial site validation — near Dai Thanh Supermarket, San Jose",
            "anchor": {
                "name": "Dai Thanh Supermarket",
                "formatted_address": "420 S 2nd St, San Jose, CA, 95113",
            },
        },
        "scored": {
            "area_score": score,
            "site_score_status": "validated" if validated else "not_validated",
            "validation_flags": {
                "lease_ready": validated,
                "commercial_listing_validated": validated,
            },
            "sub_scores": {
                "confidence_score": confidence,
                "demand_score": demand,
                "competition_gap_score": 0,
            },
            "competition_detail": {
                "competition_pressure_band": "Extreme",
            },
        },
    }


def _payload(candidate=None):
    return {
        "context": {
            "course_performance": {
                "course_enrollment_benchmarks": {
                    "strongest_historical_course_type": "AHA BLS",
                    "strongest_historical_course_type_key": "aha_bls",
                    "course_benchmarks": [{
                        "course_type": "AHA BLS",
                        "course_type_key": "aha_bls",
                        "difference_vs_allcpr_average": 1.2,
                    }],
                },
                "center_opening": {
                    "recommendations": [{
                        "course_type": "aha_bls",
                        "label": "AHA BLS",
                        "opportunity_score": 69.0,
                        "decision": "Test first",
                        "reasons": [
                            "Course performance score 91/100 from history.",
                        ],
                    }]
                }
            }
        },
        "candidates": [candidate or _candidate()],
    }


def test_unvalidated_site_cannot_open_prioritize():
    rec = build_center_recommendations_from_report(_payload(_candidate(score=92)))
    row = rec["recommendations"][0]
    assert row["decision_label"] != OPEN
    assert row["decision_label"] == TEST
    assert "not validated" in row["site_validation_status"].lower()


def test_weak_expansion_readiness_downgrades_open_recommendation():
    assert decide_center_opening(
        90,
        opportunity_score=90,
        confidence_score=90,
        readiness="Weak",
        site_validated=False,
    ) == TEST


def test_high_data_confidence_does_not_automatically_mean_open():
    rec = build_center_recommendations_from_report(_payload(_candidate(
        score=59.7, confidence=90, validated=False,
    )))
    row = rec["recommendations"][0]
    assert row["data_confidence_label"] == "Very high data confidence"
    assert row["expansion_readiness"] == "Weak"
    assert row["decision_label"] != OPEN


def test_score_below_60_does_not_open():
    rec = build_center_recommendations_from_report(_payload(_candidate(
        score=58, confidence=95, validated=True,
    )))
    assert rec["recommendations"][0]["decision_label"] != OPEN


def test_recommendation_contains_required_business_fields():
    row = build_center_recommendations_from_report(_payload())["recommendations"][0]
    for key in (
        "decision_reason",
        "main_reasons",
        "main_risks",
        "suggested_next_action",
        "warning_note",
        "evidence_summary",
    ):
        assert row[key]
    assert row["location_name"] == "Near Dai Thanh Supermarket"
    assert row["course_type"] == "AHA BLS"
    assert any("Historical benchmark leader: AHA BLS" in r
               for r in row["main_reasons"])
    assert any("historically beats ALLCPR average" in r
               for r in row["main_reasons"])


def test_json_and_csv_outputs_are_created(tmp_path: Path):
    payload = build_center_recommendations_from_report(_payload())
    jpath = tmp_path / "center_opening_recommendations.json"
    cpath = tmp_path / "center_opening_recommendations.csv"
    write_recommendations_json(payload, jpath)
    write_recommendations_csv(payload, cpath)

    loaded = json.loads(jpath.read_text(encoding="utf-8"))
    assert loaded["recommendations"][0]["decision_label"] == TEST
    with cpath.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["decision_label"] == TEST
    assert rows[0]["main_reasons"]


def test_benchmark_can_select_stronger_launch_course():
    payload = _payload()
    perf = payload["context"]["course_performance"]
    perf["center_opening"]["recommendations"] = [
        {"course_type": "aha_bls", "label": "AHA BLS",
         "opportunity_score": 69.0, "decision": "Test first", "reasons": []},
        {"course_type": "arc_bls", "label": "ARC BLS",
         "opportunity_score": 66.0, "decision": "Test first", "reasons": []},
    ]
    perf["course_enrollment_benchmarks"] = {
        "strongest_historical_course_type": "ARC BLS",
        "strongest_historical_course_type_key": "arc_bls",
        "course_benchmarks": [
            {"course_type": "AHA BLS", "course_type_key": "aha_bls",
             "average_students_per_class": 3.9,
             "difference_vs_allcpr_average": -0.1},
            {"course_type": "ARC BLS", "course_type_key": "arc_bls",
             "average_students_per_class": 4.4,
             "difference_vs_allcpr_average": 0.4},
        ],
    }
    row = build_center_recommendations_from_report(payload)["recommendations"][0]
    assert row["course_type"] == "ARC BLS"
    assert any("ARC BLS historically beats ALLCPR average" in r
               for r in row["main_reasons"])
