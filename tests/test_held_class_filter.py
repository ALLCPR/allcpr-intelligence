"""The 'held class' data-hygiene filter.

Enrollware exports carry no cancelled flag, and ~32% of rows are zero-enrolled
(cancelled / placeholder / future). These must not pollute historical averages,
benchmarks, trends, or scoring. This locks that behavior in.
"""
from __future__ import annotations

from datetime import date

from app.collectors.enrollware import (
    EnrollwareClassRecord,
    held_class_cutoff_month,
    held_classes,
    is_held_class,
)
from app.evaluation.course_enrollment_benchmarks import (
    build_course_enrollment_benchmarks,
)
from app.evaluation.course_enrollment_trends import build_course_enrollment_trends

CUTOFF = "2026-06"  # pretend "today" is somewhere in June 2026


def _rec(enrolled, capacity=12, month="2025-01", course_type="arc_cpr",
         cancelled=None):
    return EnrollwareClassRecord(
        class_name=course_type, course_type=course_type,
        course_type_label=course_type, date=f"{month}-15", month=month,
        enrolled=enrolled, capacity=capacity, cancelled=cancelled,
    )


def test_zero_and_blank_enrollment_excluded():
    assert is_held_class(_rec(5), CUTOFF) is True
    assert is_held_class(_rec(0), CUTOFF) is False       # never ran
    assert is_held_class(_rec(None), CUTOFF) is False     # no enrollment recorded


def test_future_and_current_partial_months_excluded():
    assert is_held_class(_rec(6, month="2026-05"), CUTOFF) is True   # completed
    assert is_held_class(_rec(6, month="2026-06"), CUTOFF) is False  # partial now
    assert is_held_class(_rec(6, month="2026-09"), CUTOFF) is False  # future


def test_undated_class_with_real_enrollment_is_kept():
    r = EnrollwareClassRecord(
        class_name="x", course_type="arc_cpr", course_type_label="x",
        date=None, month=None, enrolled=7,
    )
    assert is_held_class(r, CUTOFF) is True


def test_cancelled_excluded_when_known():
    assert is_held_class(_rec(5, cancelled=True), CUTOFF) is False


def test_held_classes_helper_filters_a_batch():
    recs = [_rec(5), _rec(0), _rec(6, month="2026-09"), _rec(8)]
    kept = held_classes(recs, today=date(2026, 6, 10))
    assert len(kept) == 2
    assert all(r.enrolled and r.enrolled > 0 for r in kept)


def test_cutoff_month_format():
    assert held_class_cutoff_month(date(2026, 6, 10)) == "2026-06"


def test_benchmark_excludes_phantom_and_future_classes():
    # Real held classes + phantom zeros + a future class; only the real ones count.
    recs = (
        [_rec(7, course_type="arc_cpr")] * 3
        + [_rec(0, capacity=0, course_type="arc_cpr")] * 5      # phantoms
        + [_rec(6, month="2026-09", course_type="arc_cpr")] * 4  # future
    )
    payload = build_course_enrollment_benchmarks(recs)
    arc = next(r for r in payload["course_benchmarks"]
               if r["course_type_key"] == "arc_cpr")
    assert arc["class_count"] == 3          # only the held classes
    assert arc["average_students_per_class"] == 7.0  # not dragged toward 0


def test_trends_exclude_zero_enrolled_classes():
    # Each completed month has a real class (8) and a phantom (0); the phantom
    # must not halve the monthly average.
    recs = []
    for m in range(1, 5):
        recs += [_rec(8, month=f"2025-{m:02d}")]
        recs += [_rec(0, capacity=0, month=f"2025-{m:02d}")]
    payload = build_course_enrollment_trends(recs, today=date(2026, 6, 10))
    arc = next(t for t in payload["trends"] if t["course_type_key"] == "arc_cpr")
    assert all(p["average_enrollment"] == 8.0 for p in arc["points"])
    assert arc["total_classes"] == 4
