"""Tests for Enrollware-only course enrollment benchmarks."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.collectors.enrollware import COURSE_TYPE_LABELS, EnrollwareClassRecord
from app.evaluation.course_enrollment_benchmarks import (
    build_course_enrollment_benchmarks,
    write_benchmarks_csv,
    write_benchmarks_json,
)


def _rec(course_type: str, enrolled: int, cancelled: bool = False,
         capacity: int | None = None):
    return EnrollwareClassRecord(
        class_name=COURSE_TYPE_LABELS.get(course_type, course_type),
        course_type=course_type,
        course_type_label=COURSE_TYPE_LABELS.get(course_type, course_type),
        enrolled=enrolled,
        capacity=capacity,
        cancelled=cancelled,
    )


def _by_label(payload):
    return {row["course_type"]: row for row in payload["course_benchmarks"]}


def test_allcpr_overall_average_uses_enrollware_history_only():
    payload = build_course_enrollment_benchmarks([
        _rec("arc_cpr", 4),
        _rec("arc_bls", 8),
        _rec("aha_bls", 6),
        _rec("arc_cpr", 99, cancelled=True),  # skipped
        _rec("unknown_course_type", 20),      # skipped
    ])
    assert payload["allcpr_overall_average"] == 6.0
    assert payload["allcpr_class_count"] == 3


def test_arc_cpr_arc_bls_aha_bls_are_calculated_separately():
    payload = build_course_enrollment_benchmarks([
        _rec("arc_cpr", 4), _rec("arc_cpr", 6),
        _rec("arc_bls", 8), _rec("arc_bls", 10),
        _rec("aha_bls", 5), _rec("aha_bls", 7),
    ])
    rows = _by_label(payload)
    assert rows["ARC CPR"]["average_students_per_class"] == 5.0
    assert rows["ARC BLS"]["average_students_per_class"] == 9.0
    assert rows["AHA BLS"]["average_students_per_class"] == 6.0


def test_comparison_vs_allcpr_average_is_correct():
    payload = build_course_enrollment_benchmarks([
        _rec("arc_cpr", 4), _rec("arc_cpr", 6),
        _rec("arc_bls", 8), _rec("arc_bls", 10),
        _rec("aha_bls", 5), _rec("aha_bls", 7),
    ])
    rows = _by_label(payload)
    # Overall average = 40 / 6 = 6.67; ARC BLS avg = 9.0.
    assert rows["ARC BLS"]["comparison_vs_allcpr_average"] == 2.33
    assert rows["ARC BLS"]["percent_above_or_below_allcpr_average"] == 34.9


def test_low_sample_size_is_marked_low_confidence():
    payload = build_course_enrollment_benchmarks([_rec("arc_bls", 9)])
    assert _by_label(payload)["ARC BLS"]["data_confidence"] == "low"


def test_json_and_csv_benchmark_files_are_created(tmp_path: Path):
    payload = build_course_enrollment_benchmarks([
        _rec("arc_cpr", 4), _rec("arc_bls", 8), _rec("aha_bls", 6),
    ])
    jpath = tmp_path / "course_enrollment_benchmarks.json"
    cpath = tmp_path / "course_enrollment_benchmarks.csv"
    write_benchmarks_json(payload, jpath)
    write_benchmarks_csv(payload, cpath)

    assert json.loads(jpath.read_text(encoding="utf-8"))["allcpr_overall_average"] == 6.0
    with cpath.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["course_type"] for r in rows} == {"ARC CPR", "ARC BLS", "AHA BLS"}


def _sample_payload():
    # ARC CPR avg 4.68, ARC BLS 4.21, AHA BLS 3.86, overall ~4.0 (matches the
    # numbers the boss asked the chart to reproduce).
    recs = (
        [_rec("arc_cpr", 5)] * 60 + [_rec("arc_cpr", 4)] * 40   # 4.6
        + [_rec("arc_bls", 4)] * 50 + [_rec("arc_bls", 5)] * 30  # 4.375
        + [_rec("aha_bls", 4)] * 70 + [_rec("aha_bls", 3)] * 30  # 3.7
    )
    return build_course_enrollment_benchmarks(recs)


def test_benchmark_differences_vs_allcpr_average_are_correct():
    payload = _sample_payload()
    overall = payload["allcpr_overall_average"]
    rows = _by_label(payload)
    for r in rows.values():
        expected = round(r["average_students_per_class"] - overall, 2)
        assert r["difference_vs_allcpr_average"] == expected
    # ARC CPR above benchmark, AHA BLS below it.
    assert rows["ARC CPR"]["difference_vs_allcpr_average"] > 0
    assert rows["AHA BLS"]["difference_vs_allcpr_average"] < 0


def test_csv_has_allcpr_average_and_confidence_label_columns(tmp_path: Path):
    cpath = tmp_path / "course_enrollment_benchmarks.csv"
    write_benchmarks_csv(_sample_payload(), cpath)
    with cpath.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        rows = list(reader)
    for col in ("allcpr_overall_average", "difference_vs_allcpr_average",
                "percent_vs_allcpr_average", "confidence_label",
                "average_fill_rate_pct", "allcpr_overall_fill_rate_pct",
                "recommendation_note"):
        assert col in header
    assert all(r["confidence_label"] in ("High", "Low") for r in rows)


def test_fill_rate_uses_capacity_and_ignores_blank_capacity():
    # ARC BLS: 6/12 and 9/12 -> mean fill (50% + 75%) / 2 = 62.5%.
    # The blank-capacity class still counts toward the average enrollment but
    # not toward fill rate (capacity is never invented).
    payload = build_course_enrollment_benchmarks([
        _rec("arc_bls", 6, capacity=12),
        _rec("arc_bls", 9, capacity=12),
        _rec("arc_bls", 8, capacity=None),
        _rec("arc_cpr", 3, capacity=10),
    ])
    rows = _by_label(payload)
    assert rows["ARC BLS"]["average_fill_rate_pct"] == 62.5
    assert rows["ARC BLS"]["fill_rate_class_count"] == 2
    assert payload["allcpr_overall_fill_rate_pct"] is not None


def test_no_bar_chart_field_remains():
    # The benchmark bar chart was replaced by the regression-trend section, so
    # the benchmark payload no longer carries chart/verdict data.
    payload = _sample_payload()
    assert "chart" not in payload
