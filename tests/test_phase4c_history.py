"""
Tests for the historical-intelligence layer (STEPs 5, 6, 8, 10).

Covers location performance, the historical performance score, schedule
intelligence, and the forecasting layer. All use synthetic
``EnrollwareClassRecord`` objects — no Excel/pandas, no network — and assert the
two invariants that matter for this project: deterministic output, and unknowns
stay unknown (``None``) rather than being invented.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.enrollware import (  # noqa: E402
    COURSE_TYPE_LABELS,
    EnrollwareClassRecord,
    classify_course_type,
)
from app.enrichers.location_performance import build_location_performance  # noqa: E402
from app.enrichers.schedule_intelligence import build_schedule_intelligence  # noqa: E402
from app.scoring.forecasting import build_forecast  # noqa: E402
from app.scoring.historical_performance_score import (  # noqa: E402
    score_historical_performance,
)


def rec(name, date=None, enrolled=None, capacity=None, price=None,
        city="Santa Clara", location=None, status="Completed"):
    ct = classify_course_type(name)
    dt = datetime.strptime(date, "%Y-%m-%d") if date else None
    return EnrollwareClassRecord(
        class_name=name,
        course_type=ct,
        course_type_label=COURSE_TYPE_LABELS.get(ct, ct),
        date=date,
        day_part=("weekend" if dt and dt.weekday() >= 5 else "weekday") if dt else None,
        month=dt.strftime("%Y-%m") if dt else None,
        enrolled=enrolled,
        capacity=capacity,
        price=price,
        city=city,
        location=location,
        status=status,
        cancelled=status.lower().startswith("cancel"),
    )


# A small but realistic Santa Clara BLS history: rising enrollment over 2025.
def _santa_clara_history():
    return [
        rec("AHA BLS Provider", "2025-01-11", 5, 12, 85),   # Sat
        rec("AHA BLS Provider", "2025-02-12", 6, 12, 85),   # Wed
        rec("AHA BLS Provider", "2025-04-15", 8, 12, 85),
        rec("ARC Adult CPR/AED", "2025-05-10", 4, 12, 70),
        rec("AHA BLS Provider", "2025-07-19", 10, 12, 85),  # Sat
        rec("AHA BLS Provider", "2025-09-13", 11, 12, 85),  # Sat
        rec("ARC Adult CPR/AED", "2025-10-08", 3, 12, 70),
        rec("AHA BLS Provider", "2025-11-15", 12, 12, 85),
    ]


# --------------------------------------------------------------------------- #
# Historical performance score (STEP 6)
# --------------------------------------------------------------------------- #

def test_historical_none_when_empty():
    assert score_historical_performance([]) is None


def test_historical_none_when_too_few():
    # Only 2 classes with real enrollment -> below the floor.
    recs = [rec("AHA BLS Provider", "2025-01-11", 5),
            rec("AHA BLS Provider", "2025-02-11", 6)]
    assert score_historical_performance(recs) is None


def test_historical_happy_path():
    out = score_historical_performance(_santa_clara_history())
    assert out is not None
    assert 0 <= out["score"] <= 100
    assert out["confidence"] in ("low", "medium", "high")
    assert out["reasons"]
    assert "enrollment" in out["components"]
    assert "fill_rate" in out["components"]   # capacity present
    assert "growth" in out["components"]      # multiple months
    assert out["sample_size"] == 8


def test_historical_reference_anchors_enrollment():
    recs = _santa_clara_history()
    # Avg students here ~7.4; with a low reference of 4 the enrollment factor
    # should clear parity (>50).
    out = score_historical_performance(recs, reference_avg=4.0)
    assert out["components"]["enrollment"]["score"] > 50


def test_historical_small_sample_pulls_toward_neutral():
    strong = [rec("AHA BLS Provider", f"2025-0{i}-11", 12, 12, 85) for i in range(1, 4)]
    out = score_historical_performance(strong)
    # 3 perfect-fill classes would score very high un-shrunk; the small-sample
    # discount keeps it well below a saturated 100.
    assert out is not None
    assert out["score"] < 90
    assert out["confidence"] == "low"


# --------------------------------------------------------------------------- #
# Location performance (STEP 5)
# --------------------------------------------------------------------------- #

def test_location_none_when_empty():
    assert build_location_performance([]) is None


def test_location_group_by_city():
    recs = _santa_clara_history() + [
        rec("AHA BLS Provider", "2025-03-11", 9, 12, 85, city="San Jose"),
        rec("AHA BLS Provider", "2025-06-11", 7, 12, 85, city="San Jose"),
    ]
    out = build_location_performance(recs, group_by="city")
    assert out["group_by"] == "city"
    keys = {g["key"] for g in out["groups"]}
    assert {"Santa Clara", "San Jose"} <= keys
    sc = next(g for g in out["groups"] if g["key"] == "Santa Clara")
    assert sc["utilization_percent"] is not None
    assert sc["average_students_per_class"] is not None
    assert sc["top_courses"]              # named courses present
    assert sc["revenue_estimate"] is not None  # export prices present


def test_location_group_by_course_type_uses_labels():
    out = build_location_performance(_santa_clara_history(), group_by="course_type")
    labels = {g["key"] for g in out["groups"]}
    assert "AHA BLS" in labels and "ARC CPR" in labels


def test_location_invalid_group_by_falls_back_to_city():
    out = build_location_performance(_santa_clara_history(), group_by="bogus")
    assert out["group_by"] == "city"


def test_location_revenue_unknown_when_no_price_and_no_model():
    recs = [rec("AHA BLS Provider", "2025-01-11", 5, 12, price=None),
            rec("AHA BLS Provider", "2025-02-11", 6, 12, price=None)]
    out = build_location_performance(recs, group_by="city")
    assert out["groups"][0]["revenue_estimate"] is None


# --------------------------------------------------------------------------- #
# Schedule intelligence (STEP 8)
# --------------------------------------------------------------------------- #

def test_schedule_none_when_no_dates():
    recs = [rec("AHA BLS Provider", date=None, enrolled=8)]
    assert build_schedule_intelligence(recs) is None


def test_schedule_learns_best_day_and_keeps_time_unknown():
    out = build_schedule_intelligence(_santa_clara_history())
    assert out is not None
    assert out["best_day"] is not None
    assert out["best_time"] is None          # never invented
    assert out["recommendations"]
    # Saturdays carry the strongest BLS enrollments in the fixture.
    assert out["best_day"]["basis"] == "enrollment"


def test_schedule_volume_basis_when_no_enrollment():
    recs = [
        rec("AHA BLS Provider", "2025-01-11", enrolled=None),  # Sat
        rec("AHA BLS Provider", "2025-01-18", enrolled=None),  # Sat
        rec("AHA BLS Provider", "2025-02-12", enrolled=None),  # Wed
    ]
    out = build_schedule_intelligence(recs)
    assert out["best_day"]["basis"] == "volume"
    assert out["best_day"]["average_students_per_class"] is None


# --------------------------------------------------------------------------- #
# Forecasting (STEP 10)
# --------------------------------------------------------------------------- #

def test_forecast_none_when_empty():
    assert build_forecast([]) is None


def test_forecast_recency_weighting_favors_recent():
    # Older classes enroll 4; recent classes enroll 12. A plain mean is 8;
    # recency weighting should pull the expectation above 8.
    recs = [
        rec("AHA BLS Provider", "2025-01-11", 4, 12),
        rec("AHA BLS Provider", "2025-02-11", 4, 12),
        rec("AHA BLS Provider", "2025-10-11", 12, 12),
        rec("AHA BLS Provider", "2025-11-11", 12, 12),
    ]
    out = build_forecast(recs)
    bls = next(c for c in out["course_types"] if c["course_type"] == "aha_bls")
    assert bls["expected_students"] > 8.0
    assert out["schema_version"] >= 1
    assert out["ml_ready"] is False


def test_forecast_revenue_uses_modeled_price_when_no_export_price():
    recs = [rec("AHA BLS Provider", "2025-01-11", 8, 12, price=None),
            rec("AHA BLS Provider", "2025-02-11", 10, 12, price=None)]
    out = build_forecast(recs, modeled_price=85.0)
    bls = next(c for c in out["course_types"] if c["course_type"] == "aha_bls")
    assert bls["revenue_basis"] == "modeled_allcpr_median"
    assert bls["expected_revenue"] is not None


def test_forecast_fill_rate_unknown_without_capacity():
    recs = [rec("AHA BLS Provider", "2025-01-11", 8, capacity=None),
            rec("AHA BLS Provider", "2025-02-11", 10, capacity=None)]
    out = build_forecast(recs)
    bls = next(c for c in out["course_types"] if c["course_type"] == "aha_bls")
    assert bls["expected_fill_rate_percent"] is None
