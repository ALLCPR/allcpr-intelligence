"""Tests for the Score vs Actual Enrollment Validation regression graph."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports.html_report import _course_performance_section  # noqa: E402
from app.reports.json_report import render_json  # noqa: E402
from app.reports.markdown_report import (  # noqa: E402
    _regression_validation_markdown,
)
from app.scoring.regression_validation import (  # noqa: E402
    build_regression_validation,
    simple_linear_regression,
)


# --- pure regression maths -------------------------------------------------

def test_simple_regression_slope_and_intercept():
    # y = 2x + 1 exactly.
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [3.0, 5.0, 7.0, 9.0]
    slope, intercept, r2 = simple_linear_regression(xs, ys)
    assert abs(slope - 2.0) < 1e-9
    assert abs(intercept - 1.0) < 1e-9


def test_r_squared_is_one_on_perfect_linear_data():
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    ys = [5.0, 9.0, 13.0, 17.0, 21.0]  # y = 0.4x + 1
    _, _, r2 = simple_linear_regression(xs, ys)
    assert r2 is not None
    assert abs(r2 - 1.0) < 1e-9


def test_regression_none_when_no_score_spread():
    # All x identical → no line can be fit.
    assert simple_linear_regression([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


# --- point extraction / skipping ------------------------------------------

def _perf(course_types):
    return {"area_label": "Milpitas, CA", "course_types": course_types}


def test_missing_enrollment_rows_are_skipped():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": 9.0},
        {"course_type": "b", "label": "B", "course_performance_score": 60,
         "average_students_per_class": None, "total_students": None},  # dropped
    ])
    rv = build_regression_validation(perf)
    assert rv["n"] == 1
    assert all(p["course_type"] != "b" for p in rv["points"])


def test_missing_score_rows_are_skipped():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": 9.0},
        {"course_type": "b", "label": "B", "course_performance_score": None,
         "average_students_per_class": 4.0},  # no score → dropped
    ])
    rv = build_regression_validation(perf)
    assert rv["n"] == 1
    assert rv["points"][0]["course_type"] == "a"


def test_total_enrollment_used_when_average_missing():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": None, "total_students": 120},
    ])
    rv = build_regression_validation(perf)
    assert rv["n"] == 1
    assert rv["points"][0]["actual_enrollment"] == 120.0
    assert rv["points"][0]["enrollment_basis"] == "total_students"


def test_regression_points_include_business_identity_fields():
    rv = build_regression_validation(_perf([
        {"course_type": "arc_bls", "label": "ARC BLS",
         "course_performance_score": 68.5,
         "average_students_per_class": 6.7,
         "total_classes": 12},
    ]))
    point = rv["points"][0]
    assert point["label"] == "Milpitas, CA — ARC BLS — Milpitas, CA"
    assert point["city"] == "Milpitas, CA"
    assert point["location"] == "Milpitas, CA"
    assert point["course_type"] == "arc_bls"
    assert point["score"] == 68.5
    assert point["actual_enrollment"] == 6.7
    assert point["historical_class_count"] == 12
    assert point["enrollment_basis"] == "average_students_per_class"


# --- enough-data gating ----------------------------------------------------

def test_fewer_than_three_points_returns_no_regression():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": 9.0},
        {"course_type": "b", "label": "B", "course_performance_score": 40,
         "average_students_per_class": 3.0},
    ])
    rv = build_regression_validation(perf)
    assert rv["n"] == 2
    assert rv["enough_data"] is False
    assert rv["slope"] is None and rv["intercept"] is None
    assert rv["warning"] == \
        "Not enough historical outcome data for reliable regression."


def test_three_points_fits_regression_and_correlations():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": 9.0},
        {"course_type": "b", "label": "B", "course_performance_score": 60,
         "average_students_per_class": 6.0},
        {"course_type": "c", "label": "C", "course_performance_score": 40,
         "average_students_per_class": 3.0},
    ])
    rv = build_regression_validation(perf)
    assert rv["enough_data"] is True
    assert rv["n"] == 3
    assert rv["slope"] is not None and rv["intercept"] is not None
    assert rv["r_squared"] is not None
    assert rv["pearson"] is not None
    assert rv["spearman"] is not None
    # Perfectly monotonic linear data → strong positive alignment.
    assert rv["pearson"] > 0.99


def test_prefers_evaluation_graph_final_score_over_perf_score():
    perf = _perf([
        {"course_type": "a", "label": "A", "course_performance_score": 10,
         "average_students_per_class": 9.0},
        {"course_type": "b", "label": "B", "course_performance_score": 10,
         "average_students_per_class": 3.0},
        {"course_type": "c", "label": "C", "course_performance_score": 10,
         "average_students_per_class": 6.0},
    ])
    perf["evaluation_graph"] = {"course_opportunity_graph": [
        {"course_type": "a", "label": "A", "final_score": 85},
        {"course_type": "b", "label": "B", "final_score": 30},
        {"course_type": "c", "label": "C", "final_score": 60},
    ]}
    rv = build_regression_validation(perf)
    scores = sorted(p["score"] for p in rv["points"])
    assert scores == [30.0, 60.0, 85.0]  # graph scores, not the flat perf 10s


# --- report integration ----------------------------------------------------

def _perf_with_regression():
    return _perf([
        {"course_type": "arc_cpr", "label": "ARC CPR",
         "course_performance_score": 85, "average_students_per_class": 9.0,
         "performance_band": "A", "total_classes": 40},
        {"course_type": "bls", "label": "BLS",
         "course_performance_score": 60, "average_students_per_class": 6.0,
         "performance_band": "B", "total_classes": 20},
        {"course_type": "skills", "label": "Skills Session",
         "course_performance_score": 30, "average_students_per_class": 2.5,
         "performance_band": "D", "total_classes": 8},
    ])


def test_html_contains_validation_section():
    perf = _perf_with_regression()
    perf["regression_validation"] = build_regression_validation(perf)
    html = _course_performance_section({"course_performance": perf})
    assert "Score vs Actual Enrollment Validation" in html
    # The scatter SVG and a regression line are present (3 usable points).
    assert "<svg" in html and "</svg>" in html
    assert "circle" in html  # scatter dots
    assert "<title>Milpitas, CA — ARC CPR — Milpitas, CA | Score 85.0" in html
    assert "class='reg-point'" in html
    assert "class='reg-tooltip'" in html
    assert "Score 85.0 · Avg 9.0 · Classes 40" in html
    assert "Validation points" in html
    assert "Historical class count" in html
    assert "average_students_per_class" in html
    # Stats surfaced near the graph.
    assert "R²" in html and "Pearson" in html and "Spearman" in html


def test_validation_caption_clarifies_dot_is_a_course_type():
    perf = _perf_with_regression()
    perf["regression_validation"] = build_regression_validation(perf)
    html = _course_performance_section({"course_performance": perf})
    # Each dot is a course type for the area, not a candidate location, and the
    # caption points readers to where candidate locations actually live.
    assert "Milpitas, CA course type" in html
    assert "not one candidate location" in html
    assert "validates course scoring against Enrollware history" in html
    assert "Center Opening Recommendation table and map" in html


def test_hybrid_course_label_and_explanation_render():
    from app.enrichers.course_classifier import (
        COURSE_TYPE_LABELS, HYBRID_COURSE_NOTE,
    )
    # The renamed label, no longer "(blended)".
    assert COURSE_TYPE_LABELS["cpr_first_aid_blended"] == "CPR / First Aid (hybrid)"
    perf = _perf([
        {"course_type": "cpr_first_aid_blended",
         "label": COURSE_TYPE_LABELS["cpr_first_aid_blended"],
         "course_performance_score": 50, "average_students_per_class": 5.0,
         "performance_band": "C", "total_classes": 30},
    ])
    html = _course_performance_section({"course_performance": perf})
    assert "CPR / First Aid (hybrid)" in html
    assert "(blended)" not in html
    # The plain-English hybrid definition is surfaced.
    assert "online CPR/First Aid lessons" in html
    assert HYBRID_COURSE_NOTE in html


def test_html_does_not_claim_future_prediction_certainty():
    perf = _perf_with_regression()
    perf["regression_validation"] = build_regression_validation(perf)
    html = _course_performance_section({"course_performance": perf})
    low = html.lower()
    # Honest framing present.
    assert "validation only" in low
    assert "not a future guarantee" in low
    assert ("future results may change due to ads, price, schedule timing, "
            "student behavior, and competition.") in low
    # And it never promises a guaranteed/certain future outcome.
    assert "guaranteed prediction" not in low
    assert "will enroll" not in low


def test_json_contains_regression_validation():
    perf = _perf_with_regression()
    payload = render_json([], {"course_performance": perf})
    assert "regression_validation" in payload
    rv = payload["regression_validation"]
    assert rv["x_label"] == "Opportunity score"
    assert rv["y_label"] == "Actual historical enrollment"
    assert rv["enough_data"] is True
    assert isinstance(rv["points"], list) and len(rv["points"]) == 3
    assert rv["validation_only"] is True
    assert {"label", "city", "course_type", "score",
            "actual_enrollment"}.issubset(rv["points"][0])


def test_markdown_renders_validation_section():
    perf = _perf_with_regression()
    rv = build_regression_validation(perf)
    md = "\n".join(_regression_validation_markdown(rv))
    assert "Score vs Actual Enrollment Validation" in md
    assert "Validation only" in md
    assert "R²" in md
    assert "ARC CPR" in md


def test_markdown_few_points_shows_warning_not_line():
    rv = build_regression_validation(_perf([
        {"course_type": "a", "label": "A", "course_performance_score": 80,
         "average_students_per_class": 9.0},
    ]))
    md = "\n".join(_regression_validation_markdown(rv))
    assert "Not enough historical outcome data" in md
    assert "No regression line drawn" in md
