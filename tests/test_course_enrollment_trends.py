"""Tests for the Enrollware-only historical enrollment trend regression."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.collectors.enrollware import COURSE_TYPE_LABELS, EnrollwareClassRecord
from app.evaluation.course_enrollment_trends import (
    build_course_enrollment_trends,
    write_trends_csv,
    write_trends_json,
)


def _rec(course_type: str, month_idx: int, enrolled: int,
         cancelled: bool = False):
    """One class in month YYYY-MM derived from a 1-based month index."""
    year = 2025 + (month_idx - 1) // 12
    month = (month_idx - 1) % 12 + 1
    date = f"{year}-{month:02d}-15"
    return EnrollwareClassRecord(
        class_name=COURSE_TYPE_LABELS.get(course_type, course_type),
        course_type=course_type,
        course_type_label=COURSE_TYPE_LABELS.get(course_type, course_type),
        date=date,
        month=f"{year}-{month:02d}",
        enrolled=enrolled,
        cancelled=cancelled,
    )


def _improving():
    # Rising one student/class every month across 12 months, 3 courses.
    recs = []
    for m in range(1, 13):
        recs += [_rec("arc_cpr", m, 2 + m)]          # clearly improving
        recs += [_rec("arc_bls", m, 6)]              # flat
        recs += [_rec("aha_bls", m, max(1, 14 - m))]  # clearly declining
    return recs


def _by_key(payload):
    return {t["course_type_key"]: t for t in payload["trends"]}


def test_three_course_types_are_calculated_from_enrollware():
    payload = build_course_enrollment_trends(_improving())
    assert payload["basis"] == "Enrollware historical enrollment only"
    keys = _by_key(payload)
    assert {"arc_cpr", "arc_bls", "aha_bls"} <= set(keys)
    for t in keys.values():
        assert t["basis"] == "monthly_average_enrollment"
        assert t["n"] == 12
        assert t["total_classes"] == 12


def test_regression_stats_present_and_directions_correct():
    keys = _by_key(build_course_enrollment_trends(_improving()))
    # Every course exposes the four regression-stat keys.
    for t in keys.values():
        for stat in ("slope", "intercept", "r_squared", "pearson"):
            assert stat in t
        assert isinstance(t["slope"], (int, float))
        assert isinstance(t["intercept"], (int, float))
    # The two varying series get a real R²/Pearson; the flat constant series
    # honestly reports None (undefined correlation with zero y-variance).
    for k in ("arc_cpr", "aha_bls"):
        assert isinstance(keys[k]["r_squared"], (int, float))
        assert isinstance(keys[k]["pearson"], (int, float))
    assert keys["arc_cpr"]["slope"] > 0
    assert keys["arc_cpr"]["trend_direction"] == "improving"
    assert keys["aha_bls"]["slope"] < 0
    assert keys["aha_bls"]["trend_direction"] == "declining"
    assert keys["arc_bls"]["trend_direction"] == "flat"
    assert keys["arc_bls"]["r_squared"] is None


def test_too_few_points_returns_insufficient_data_not_fake_regression():
    # Only two months for ARC CPR -> below MIN_TREND_POINTS, no line fit.
    payload = build_course_enrollment_trends([
        _rec("arc_cpr", 1, 5), _rec("arc_cpr", 2, 6),
    ])
    cpr = _by_key(payload)["arc_cpr"]
    assert cpr["trend_direction"] == "insufficient data"
    assert cpr["slope"] is None
    assert cpr["r_squared"] is None
    assert cpr["pearson"] is None
    assert cpr["confidence_label"] == "Insufficient"


def test_individual_class_fallback_when_months_too_few():
    # Three classes, all in the same month -> only 1 monthly point, so the
    # builder falls back to per-class records (3 dated points) for the fit.
    payload = build_course_enrollment_trends([
        EnrollwareClassRecord(
            class_name="ARC", course_type="arc_cpr", course_type_label="ARC CPR",
            date=f"2025-01-{d:02d}", month="2025-01", enrolled=e,
        )
        for d, e in ((5, 3), (12, 5), (20, 7))
    ])
    cpr = _by_key(payload)["arc_cpr"]
    assert cpr["basis"] == "individual_class_records"
    assert cpr["n"] == 3
    assert cpr["trend_direction"] == "improving"


def test_cancelled_and_unknown_excluded():
    payload = build_course_enrollment_trends([
        _rec("arc_cpr", 1, 5),
        _rec("arc_cpr", 2, 99, cancelled=True),   # skipped
        _rec("unknown_course_type", 1, 50),        # skipped
    ])
    cpr = _by_key(payload)["arc_cpr"]
    assert cpr["total_classes"] == 1


def test_json_and_csv_trend_files_are_created(tmp_path: Path):
    payload = build_course_enrollment_trends(_improving())
    jpath = tmp_path / "course_enrollment_trends.json"
    cpath = tmp_path / "course_enrollment_trends.csv"
    write_trends_json(payload, jpath)
    write_trends_csv(payload, cpath)

    loaded = json.loads(jpath.read_text(encoding="utf-8"))
    assert loaded["trends"][0]["points"]
    with cpath.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        rows = list(reader)
    for col in ("course_type", "period", "average_enrollment", "class_count",
                "trend_direction", "slope", "r_squared", "pearson",
                "confidence_label"):
        assert col in header
    assert {r["course_type"] for r in rows} == {"ARC CPR", "ARC BLS", "AHA BLS"}


def test_building_trends_does_not_mutate_records():
    recs = _improving()
    before = [(r.course_type, r.enrolled, r.month) for r in recs]
    build_course_enrollment_trends(recs)
    after = [(r.course_type, r.enrolled, r.month) for r in recs]
    assert before == after
