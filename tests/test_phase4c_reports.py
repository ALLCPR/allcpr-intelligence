"""
Integration test for the Phase 4C report wiring (STEP 9).

Verifies that ``build_course_performance_section`` attaches the new
schedule-intelligence, forecast, and location-performance payloads, and that
the HTML course-performance section renders the new subsections without
inventing data.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.enrollware import (  # noqa: E402
    COURSE_TYPE_LABELS,
    EnrollwareClassRecord,
    classify_course_type,
)
from app.reports.html_report import _course_performance_section  # noqa: E402
from app.reports.interpretation import build_course_performance_section  # noqa: E402


def rec(name, date, enrolled=None, capacity=None, price=None, city="Santa Clara"):
    ct = classify_course_type(name)
    dt = datetime.strptime(date, "%Y-%m-%d")
    return EnrollwareClassRecord(
        class_name=name, course_type=ct,
        course_type_label=COURSE_TYPE_LABELS.get(ct, ct),
        date=date,
        day_part="weekend" if dt.weekday() >= 5 else "weekday",
        month=dt.strftime("%Y-%m"),
        enrolled=enrolled, capacity=capacity, price=price, city=city,
        status="Completed", cancelled=False,
    )


def _records():
    return [
        rec("AHA BLS Provider", "2025-01-11", 5, 12, 85),
        rec("AHA BLS Provider", "2025-04-15", 8, 12, 85),
        rec("AHA BLS Provider", "2025-07-19", 10, 12, 85),
        rec("AHA BLS Provider", "2025-11-15", 12, 12, 85),
        rec("ARC Adult CPR/AED", "2025-05-10", 4, 12, 70),
        rec("AHA BLS Provider", "2025-03-11", 9, 12, 85, city="San Jose"),
        rec("AHA BLS Provider", "2025-06-11", 7, 12, 85, city="San Jose"),
    ]


def test_section_attaches_phase4c_payloads():
    perf = build_course_performance_section(
        _records(), city="Santa Clara", state="CA",
        demand_counts={"hospital": 3, "gym": 1},
    )
    assert perf is not None
    # The new lenses are present and well-formed.
    assert "schedule_intelligence" in perf
    assert perf["schedule_intelligence"]["best_time"] is None  # never invented
    assert "forecast" in perf
    assert perf["forecast"]["ml_ready"] is False
    assert perf["forecast"]["course_types"]
    assert "location_performance" in perf
    # Location grouping is ALLCPR-wide, so both cities appear.
    keys = {g["key"] for g in perf["location_performance"]["groups"]}
    assert {"Santa Clara", "San Jose"} <= keys


def test_html_renders_new_subsections():
    perf = build_course_performance_section(_records(), city="Santa Clara", state="CA")
    html = _course_performance_section({"course_performance": perf})
    assert "Schedule Intelligence" in html
    assert "Forecast — Next-Class Expectation" in html
    # The honesty note about missing class times must be present.
    assert "time-of-day is left unknown" in html


def test_html_placeholder_when_no_data():
    html = _course_performance_section({})
    assert "No Enrollware class history loaded" in html
    assert "Schedule Intelligence" not in html


def _trend_records():
    """A year of monthly classes per course type so regression has real points."""
    recs = []
    months = [f"2025-{m:02d}-15" for m in range(1, 13)]
    for i, d in enumerate(months):
        recs += [rec("ARC Adult CPR/AED", d, 4 + i % 3, 12)] * 3   # ARC CPR
        recs += [rec("ARC BLS Provider", d, 8, 12)] * 3            # ARC BLS (flat)
        recs += [rec("AHA BLS Provider", d, 6, 12)] * 3            # AHA BLS
    return recs


def test_html_replaces_bar_chart_with_trend_section():
    perf = build_course_performance_section(
        _trend_records(), city="Santa Clara", state="CA")
    html = _course_performance_section({"course_performance": perf})

    # The old benchmark bar chart is gone — neither its title nor its SVG class.
    assert "Course Enrollment vs ALLCPR Benchmark" not in html
    assert "bar-svg" not in html

    # The benchmark *table* is kept (table compares the courses + ALLCPR avg).
    assert "Historical Enrollment by Course Type" in html

    # The new regression trend section + three course cards render.
    assert "Historical Enrollment Trend by Course Type" in html
    assert "trend-svg" in html
    for label in ("ARC CPR", "ARC BLS", "AHA BLS"):
        assert label in html
    # Enrollware-only basis is stated and at least one regression line is drawn.
    assert "Enrollware history only" in html

    # Trends are computed from Enrollware data only and carry regression stats.
    trends = perf["course_enrollment_trends"]["trends"]
    keys = {t["course_type_key"] for t in trends}
    assert {"arc_cpr", "arc_bls", "aha_bls"} <= keys
    for t in trends:
        for stat in ("slope", "intercept", "r_squared", "pearson",
                     "trend_direction"):
            assert stat in t
