"""Enrollware historical performance integration tests."""
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
    load_records,
)
from app.enrichers.historical_performance import (  # noqa: E402
    build_candidate_historical_performance,
)
from app.reports.html_report import _candidate_card  # noqa: E402
from app.reports.markdown_report import render_markdown_report  # noqa: E402
from app.scoring.site_score import score_profile  # noqa: E402
from tests.test_scoring import _synthetic_profile  # noqa: E402


def rec(name, date, enrolled, capacity=12, city="San Jose", state="CA",
        location="San Jose"):
    ct = classify_course_type(name)
    dt = datetime.strptime(date, "%Y-%m-%d")
    return EnrollwareClassRecord(
        class_name=name,
        course_type=ct,
        course_type_label=COURSE_TYPE_LABELS.get(ct, ct),
        date=date,
        day_part="weekend" if dt.weekday() >= 5 else "weekday",
        month=dt.strftime("%Y-%m"),
        enrolled=enrolled,
        capacity=capacity,
        city=city,
        state=state,
        location=location,
        status="Completed",
        cancelled=False,
    )


def history_records():
    return [
        rec("AHA BLS Provider", "2025-01-11", 9),
        rec("AHA BLS Provider", "2025-02-11", 10),
        rec("AHA BLS Provider", "2025-03-11", 11),
        rec("ARC Adult CPR/AED", "2025-04-11", 8),
        rec("AHA BLS Provider", "2025-05-11", 12),
        rec("AHA BLS Provider", "2025-06-11", 12),
        rec("ARC Adult CPR/AED", "2025-07-11", 7),
        rec("AHA BLS Provider", "2025-08-11", 12),
        rec("AHA BLS Provider", "2025-01-11", 3, city="Slowtown", location="Slowtown"),
        rec("AHA BLS Provider", "2025-02-11", 4, city="Slowtown", location="Slowtown"),
        rec("AHA BLS Provider", "2025-03-11", 3, city="Slowtown", location="Slowtown"),
        rec("AHA BLS Provider", "2025-04-11", 4, city="Slowtown", location="Slowtown"),
        rec("AHA BLS Provider", "2025-05-11", 3, city="Slowtown", location="Slowtown"),
    ]


def test_historical_metric_extraction_for_candidate_area():
    hist = build_candidate_historical_performance(
        history_records(), city="San Jose", state="CA"
    )

    assert hist["status"] == "scored"
    assert hist["total_classes"] == 8
    assert hist["average_students_per_class"] is not None
    assert hist["fill_rate_percent"] is not None
    assert hist["course_type_frequency"][0]["label"] == "AHA BLS"
    assert hist["recent_activity"]["latest_class_date"] == "2025-08-11"
    assert hist["strong_locations"]
    assert hist["weak_locations"]


def test_historical_matches_city_alias_after_comma():
    hist = build_candidate_historical_performance(
        history_records(), city="Santana Row, San Jose", state="CA"
    )

    assert hist["status"] == "scored"
    assert hist["match_type"] == "city_alias"
    assert hist["total_classes"] == 8


def test_location_abbreviation_resolution_from_locations_export(tmp_path: Path):
    classes = tmp_path / "classes.csv"
    classes.write_text(
        "Class Name,Class Date,Students Enrolled,Max Students,Location\n"
        "AHA BLS Provider,2026-01-12,9,12,SJ\n",
        encoding="utf-8",
    )
    locations = tmp_path / "locations.csv"
    locations.write_text(
        "Abbreviation,Name\n"
        "SJ,\"San Jose(1631 N First Street, Suite 200, San Jose, CA 95112)\"\n",
        encoding="utf-8",
    )

    records = load_records(classes, locations_path=locations)

    assert records[0].city == "San Jose"
    assert records[0].state == "CA"


def test_historical_score_boosts_and_penalizes_area_score():
    strong = _synthetic_profile(
        historical_performance={"score": 90, "status": "scored", "total_classes": 8}
    )
    weak = _synthetic_profile(
        historical_performance={"score": 20, "status": "scored", "total_classes": 8}
    )

    strong_scored = score_profile(strong)
    weak_scored = score_profile(weak)

    assert strong_scored["sub_scores"]["historical_performance_score"] == 90
    assert weak_scored["sub_scores"]["historical_performance_score"] == 20
    assert strong_scored["area_score"] > weak_scored["area_score"]


def test_historical_performance_renders_in_markdown_and_html():
    profile = _synthetic_profile(
        historical_performance=build_candidate_historical_performance(
            history_records(), city="San Jose", state="CA"
        )
    )
    scored = score_profile(profile)

    md = render_markdown_report("San Jose", "CA", 2.0, [(profile, scored)])
    html = _candidate_card({"profile": profile, "scored": scored}, "executive", 1)

    assert "Historical ALLCPR performance" in md
    assert "Course type frequency" in md
    assert "Historical ALLCPR performance" in html
    assert "AHA BLS" in html
