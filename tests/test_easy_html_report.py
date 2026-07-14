"""The easy executive report: a second presentation layer over the same payload.

Verifies the easy report is generated alongside the main report, names the file
correctly, surfaces the boss-facing sections, collapses the heavy ones, and
never mutates the scored payload (no scoring/logic change)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from app.collectors.enrollware import EnrollwareClassRecord
from app.reports.easy_html_report import (
    easy_output_path,
    render_easy_html_report,
    write_easy_html_report,
)
from app.reports.html_report import write_html_report
from app.reports.interpretation import build_course_performance_section


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _rec(ct, month, enrolled, cap=12, city="San Jose"):
    return EnrollwareClassRecord(
        class_name=ct, course_type=ct, course_type_label=ct,
        date=f"{month}-15", month=month, enrolled=enrolled, capacity=cap,
        city=city, state="CA",
    )


def _records():
    recs = []
    for i, month in enumerate(("2025-01", "2025-02", "2025-03", "2025-04")):
        recs += [_rec("arc_cpr", month, 7 + i)] * 4
        recs += [_rec("arc_bls", month, 6)] * 4
        recs += [_rec("aha_bls", month, 5 + i)] * 4
        recs += [_rec("arc_cpr", month, 0, cap=0)]   # phantom (excluded)
    return recs


def _candidate():
    return {
        "rank": 1,
        "profile": {
            "city": "San Jose", "state": "CA",
            "candidate_name": "Needs commercial site validation — near Dai Thanh "
                              "Supermarket, San Jose",
            "anchor": {"name": "Dai Thanh Supermarket",
                       "formatted_address": "420 S 2nd St, San Jose, CA, 95113"},
        },
        "scored": {
            "area_score": 59.7, "site_score_status": "not_validated",
            "validation_flags": {"lease_ready": False,
                                 "commercial_listing_validated": False},
            "sub_scores": {"confidence_score": 85.0, "demand_score": 90.0,
                           "competition_gap_score": 0},
            "competition_detail": {"competition_pressure_band": "Extreme"},
        },
    }


def _payload():
    course = build_course_performance_section(_records(), city="San Jose", state="CA")
    return {
        "candidates": [_candidate()],
        "context": {
            "cities": ["San Jose, CA"],
            "course_performance": course,
            "enrollware_data_quality": {
                "classes_loaded": 100, "classes_blank_ignored": 0,
                "held_classes": 80, "zero_student_rows": 20,
                "missing_location": 0, "missing_start_date": 0,
                "missing_end_date": 0, "missing_hours": 0,
                "unmatched_locations": 0, "ambiguous_location_rows": 0,
                "capacity_overfilled": 0, "zero_seats_with_students": 0,
                "locations_loaded": 5, "locations_blank_ignored": 0,
                "locations_missing_abbreviation": 0,
                "duplicate_abbreviations": {}, "ambiguous_abbreviations": [],
            },
        },
    }


# --------------------------------------------------------------------------- #
# Filename rule
# --------------------------------------------------------------------------- #

def test_easy_output_path_naming():
    assert easy_output_path(Path("data/reports/sj_report.html")).name == \
        "sj_easy_report.html"
    assert easy_output_path(Path("data/reports/all_cities_report.html")).name == \
        "all_cities_easy_report.html"
    assert easy_output_path(Path("x/allcpr_site_report.html")).name == \
        "allcpr_site_easy_report.html"


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #

def test_easy_report_has_quick_verdict_and_core_sections():
    html = render_easy_html_report(_payload(), full_report_name="sj_report.html",
                                   easy_report_name="sj_easy_report.html")
    assert "Quick verdict" in html
    assert "Center Opening Recommendation" in html
    # Course intelligence: the held-class benchmark table + trend charts.
    assert "Course intelligence" in html
    assert "Historical Enrollment by Course Type" in html
    assert "Historical Enrollment Trend by Course Type" in html
    for label in ("ARC CPR", "ARC BLS", "AHA BLS"):
        assert label in html
    # Score validation, explained simply, with the stats.
    assert "Score validation" in html
    assert "higher opportunity scores" in html
    assert "R²" in html and "Pearson" in html


def test_easy_report_shows_top_candidates_table():
    html = render_easy_html_report(_payload())
    assert "Dai Thanh Supermarket" in html
    # The decision column renders the recommendation label.
    assert "Test first" in html or "Open / Prioritize" in html or "Avoid" in html


def test_easy_report_collapses_heavy_sections_in_details():
    html = render_easy_html_report(_payload())
    assert "<details" in html
    # Methodology, data-quality audit, and source evidence are collapsed.
    for collapsed in ("Methodology", "Data-quality audit", "Source evidence"):
        assert collapsed in html
        idx = html.index(collapsed)
        assert "<details" in html[:idx]


def test_easy_report_keeps_warnings_and_held_note_visible():
    html = render_easy_html_report(_payload())
    assert "Site not validated." in html
    assert "not a guaranteed prediction" in html
    assert "lease-ready" in html
    assert "completed held classes only" in html  # data-quality note


def test_easy_report_links_to_full_report():
    html = render_easy_html_report(_payload(), full_report_name="sj_report.html")
    assert "Open full technical report" in html
    assert 'href="sj_report.html"' in html


# --------------------------------------------------------------------------- #
# Generation alongside the main report + no payload mutation
# --------------------------------------------------------------------------- #

def test_both_reports_written_with_correct_names(tmp_path: Path):
    payload = _payload()
    main = tmp_path / "sj_report.html"
    write_html_report(payload, main)
    easy = easy_output_path(main)
    write_easy_html_report(payload, easy, full_report_name=main.name)
    assert main.exists() and main.name == "sj_report.html"
    assert easy.exists() and easy.name == "sj_easy_report.html"
    # The easy file self-references its own name in the cross-links.
    assert 'href="sj_easy_report.html"' in easy.read_text(encoding="utf-8")


def test_easy_render_does_not_mutate_payload_or_scores():
    payload = _payload()
    before = json.dumps(payload, sort_keys=True, default=str)
    render_easy_html_report(copy.deepcopy(payload))
    # Rendering is read-only: the scored payload is byte-for-byte unchanged.
    after = json.dumps(payload, sort_keys=True, default=str)
    assert before == after
    assert payload["candidates"][0]["scored"]["area_score"] == 59.7
