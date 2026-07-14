"""
Phase 4B — Enrollware loader, course classifier, performance enricher,
course_performance_score scoring, and report-section assembly.

Uses tiny in-test CSV (and, when openpyxl is available, Excel) fixtures so the
suite never depends on the proprietary export.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import enrollware  # noqa: E402
from app.collectors.enrollware import (  # noqa: E402
    classify_course_type,
    load_records,
)
from app.enrichers.course_performance import build_course_performance  # noqa: E402
from app.scoring.course_performance_score import (  # noqa: E402
    compare_public_demand,
    score_course_performance,
)
from app.reports.interpretation import (  # noqa: E402
    aggregate_demand_counts,
    build_course_performance_section,
)
from app.reports.html_report import _course_performance_section  # noqa: E402


_CSV = """Class Name,Class Date,Students Enrolled,Max Students,Price,City,Status
AHA BLS Provider,2026-01-12,9,12,85,Santa Clara,Completed
AHA BLS Provider,2026-01-17,8,12,85,Santa Clara,Completed
AHA BLS Provider Sat,2026-01-24,11,12,85,Santa Clara,Completed
ARC Adult CPR/AED,2026-02-02,4,12,70,Santa Clara,Completed
ARC Adult CPR/AED,2026-02-21,3,12,70,Santa Clara,Cancelled
ALLCPR BLS Provider Course,2026-03-16,7,12,79,Santa Clara,Completed
Heartsaver First Aid CPR AED Blended,2026-04-04,10,16,95,Santa Clara,Completed
BLS Skills Session,2026-04-11,2,6,40,Santa Clara,Completed
BLS Skills Session,2026-04-18,1,6,40,Santa Clara,Completed
"""


@pytest.fixture(autouse=True)
def _isolate_from_real_data(monkeypatch, request):
    """Keep tests hermetic: never auto-discover the proprietary exports that
    may live in data/raw (they'd also make the suite slow)."""
    if request.node.name == "test_real_export_filenames_are_discoverable":
        return
    monkeypatch.setattr(enrollware, "LOCATIONS_FILES", [])
    monkeypatch.setattr(enrollware, "ENROLLWARE_FILES", [])


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "enrollware_classes.csv"
    p.write_text(_CSV, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,expected", [
    ("AHA BLS Provider", "aha_bls"),
    ("American Heart Association BLS for Healthcare", "aha_bls"),
    ("AHA© BLS Provider Course (Initial&Renewal)", "aha_bls"),
    ("AHA Heartsaver CPR AED", "aha_cpr"),
    ("ARC BLS for Healthcare Providers", "arc_bls"),
    ("Red Cross Basic Life Support-BL R.25", "arc_bls"),
    ("Red Cross Adult CPR/AED", "arc_cpr"),
    # Real-data: a Red Cross First Aid/CPR/AED class is ARC CPR (provider wins).
    ("Red Cross Adult/Pediatric First Aid/CPR/AED r.25", "arc_cpr"),
    ("Red Cross Adult First Aid/CPR/AED r.25", "arc_cpr"),
    # Provider-less First Aid/CPR/AED stays in the generic blended bucket.
    ("Self Directed Adult and Pediatric First Aid/CPR/AED r.25",
     "cpr_first_aid_blended"),
    ("Adult First Aid/CPR/AED", "cpr_first_aid_blended"),
    ("Heartsaver First Aid CPR AED (Blended)", "aha_cpr"),  # Heartsaver = AHA
    ("ALLCPR BLS Provider Course", "allcpr_bls"),
    # "cpr" inside the company name "AllCPR" must NOT register a CPR course.
    ("AllCPR Group Training", "unknown_course_type"),
    ("Red Cross Adult/Pediatric First Aid/CPR/AED Skill Session",
     "skills_session"),
    ("BLS Skills Session", "skills_session"),
    ("RQI Skills", "skills_session"),
    ("Some Random Lifeguard Class", "unknown_course_type"),
    ("", "unknown_course_type"),
    (None, "unknown_course_type"),
])
def test_classify_course_type(name, expected):
    assert classify_course_type(name) == expected


def test_skills_beats_provider_bls():
    # "BLS Skills Session" is a skills session, not an AHA/ARC BLS class.
    assert classify_course_type("AHA BLS Skills Session") == "skills_session"


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def test_load_records_from_csv(csv_file: Path):
    records = load_records(csv_file)
    assert len(records) == 9
    aha = [r for r in records if r.course_type == "aha_bls"]
    assert len(aha) == 3
    assert all(r.enrolled is not None for r in aha)
    # Cancelled status is detected; day_part derived from the date.
    cancelled = [r for r in records if r.cancelled]
    assert len(cancelled) == 1
    sat = [r for r in records if r.date == "2026-01-24"][0]
    assert sat.day_part == "weekend"


def test_missing_file_returns_empty(tmp_path: Path):
    assert load_records(tmp_path / "nope.csv") == []


def test_no_class_name_column_returns_empty(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("foo,bar\n1,2\n", encoding="utf-8")
    assert load_records(p) == []


def test_unknown_fields_stay_none(tmp_path: Path):
    # No capacity / price / date columns -> those stay None, never invented.
    p = tmp_path / "minimal.csv"
    p.write_text("Class Name,Students\nAHA BLS Provider,9\n", encoding="utf-8")
    records = load_records(p)
    assert len(records) == 1
    r = records[0]
    assert r.enrolled == 9
    assert r.capacity is None
    assert r.price is None
    assert r.date is None
    assert r.day_part is None


# --------------------------------------------------------------------------- #
# Enricher aggregation
# --------------------------------------------------------------------------- #

def test_build_course_performance_metrics(csv_file: Path):
    records = load_records(csv_file)
    perf = build_course_performance(records, city="Santa Clara", state="CA")
    assert perf is not None
    by_type = {c["course_type"]: c for c in perf["course_types"]}
    assert set(by_type) == {"aha_bls", "arc_cpr"}
    assert perf["course_rollup"]["dropped_by_source_course_type"] == {
        "allcpr_bls": 1,
        "aha_cpr": 1,
        "skills_session": 2,
    }

    aha = by_type["aha_bls"]
    assert aha["total_classes"] == 3
    assert aha["total_students"] == 28           # 9 + 8 + 11
    assert aha["average_students_per_class"] == round(28 / 3, 2)
    assert aha["median_students_per_class"] == 9
    # fill rate = 28 / (12*3) = 77.8%
    assert aha["fill_rate_percent"] == pytest.approx(77.8, abs=0.1)
    # revenue estimate = 28 * 85
    assert aha["revenue_estimate"] == pytest.approx(28 * 85, abs=0.01)

    arc = by_type["arc_cpr"]
    assert arc["cancelled_classes"] == 1
    assert arc["cancellation_rate_percent"] == 50.0  # 1 of 2


def test_build_course_performance_filters_city_alias(csv_file: Path):
    records = load_records(csv_file)
    perf = build_course_performance(records, city="Santana Row, Santa Clara", state="CA")
    assert perf is not None
    assert perf["total_classes"] == 5


def test_course_performance_rolls_up_provider_proven_skills_only():
    from app.collectors.enrollware import EnrollwareClassRecord, COURSE_TYPE_LABELS

    def rec(name, course_type, enrolled=4):
        return EnrollwareClassRecord(
            class_name=name,
            course_type=course_type,
            course_type_label=COURSE_TYPE_LABELS.get(course_type, course_type),
            enrolled=enrolled,
            capacity=10,
        )

    records = [
        rec("AHA BLS Skills Session", "skills_session", 3),
        rec("Red Cross Basic Life Support Skills Session", "skills_session", 4),
        rec("Red Cross Adult/Pediatric First Aid/CPR/AED Skill Session",
            "skills_session", 5),
        rec("BLS Skills Session", "skills_session", 6),
        rec("ALLCPR BLS Provider Course", "allcpr_bls", 7),
        rec("Adult First Aid/CPR/AED", "cpr_first_aid_blended", 8),
    ]

    perf = build_course_performance(records)
    by_type = {c["course_type"]: c for c in perf["course_types"]}

    assert by_type["aha_bls"]["total_students"] == 3
    assert by_type["arc_bls"]["total_students"] == 4
    assert by_type["arc_cpr"]["total_students"] == 5
    assert perf["course_rollup"]["dropped_by_source_course_type"] == {
        "skills_session": 1,
        "allcpr_bls": 1,
        "cpr_first_aid_blended": 1,
    }


def test_empty_records_returns_none():
    assert build_course_performance([]) is None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def test_score_course_performance_ranks_and_bands(csv_file: Path):
    records = load_records(csv_file)
    perf = build_course_performance(records, city="Santa Clara", state="CA")
    score_course_performance(perf, allcpr_overall_avg=6.0)

    by_type = {c["course_type"]: c for c in perf["course_types"]}
    # AHA BLS (avg ~9.3) is well above the local mean -> Strong, high score.
    aha = by_type["aha_bls"]
    assert aha["course_performance_score"] is not None
    assert aha["performance_band"] == "Strong"
    assert aha["vs_allcpr_avg"] == pytest.approx(round(28 / 3 - 6.0, 2), abs=0.01)

    strat = perf["strategy"]
    assert strat["primary"], "expected a primary course recommendation"
    assert "AHA BLS" in strat["primary"][0]
    assert not any("Skills" in s for s in strat["avoid_or_test"])
    assert perf["scheduling_recommendations"]


def test_unknown_course_type_never_primary():
    """The un-classifiable bucket must not become a course recommendation,
    even when it scores high on a couple of flukey high-enrollment rows."""
    from app.collectors.enrollware import EnrollwareClassRecord, COURSE_TYPE_LABELS

    def rec(ct, enrolled, cap=12):
        return EnrollwareClassRecord(
            class_name=ct, course_type=ct,
            course_type_label=COURSE_TYPE_LABELS.get(ct, ct),
            enrolled=enrolled, capacity=cap,
        )
    records = [
        rec("unknown_course_type", 11), rec("unknown_course_type", 12),
        rec("aha_bls", 8), rec("aha_bls", 9), rec("aha_bls", 7),
    ]
    perf = build_course_performance(records)
    score_course_performance(perf)
    strat = perf["strategy"]
    unknown_label = COURSE_TYPE_LABELS["unknown_course_type"]
    assert unknown_label not in strat["primary"]
    assert unknown_label not in strat["avoid_or_test"]
    assert "AHA BLS" in strat["primary"]


def test_unknown_enrollment_scores_none():
    from app.collectors.enrollware import EnrollwareClassRecord, COURSE_TYPE_LABELS
    rec = EnrollwareClassRecord(
        class_name="AHA BLS Provider", course_type="aha_bls",
        course_type_label=COURSE_TYPE_LABELS["aha_bls"], enrolled=None,
    )
    perf = build_course_performance([rec])
    score_course_performance(perf)
    ct = perf["course_types"][0]
    assert ct["average_students_per_class"] is None
    assert ct["course_performance_score"] is None
    assert ct["performance_band"] == "Unknown"


def test_compare_public_demand(csv_file: Path):
    records = load_records(csv_file)
    perf = build_course_performance(records, city="Santa Clara", state="CA")
    score_course_performance(perf)
    demand = {"hospital": 18, "nursing_school": 6, "gym": 20}
    cmp = compare_public_demand(perf, demand)
    assert cmp["bls_demand_sites"] == 24      # 18 + 6
    assert cmp["bls_actual_avg_students"] is not None
    assert cmp["notes"]


# --------------------------------------------------------------------------- #
# Section assembly + HTML rendering
# --------------------------------------------------------------------------- #

def test_build_section_and_render(csv_file: Path):
    records = load_records(csv_file)
    ranked = [({"counts_5mi": {"hospital": 18, "nursing_school": 6}}, {})]
    section = build_course_performance_section(
        records, city="Santa Clara", state="CA",
        demand_counts=aggregate_demand_counts(ranked),
    )
    assert section is not None
    assert section["scored"] is True
    html = _course_performance_section({"course_performance": section})
    # Detailed tables still render (now folded into a <details> block).
    assert "Historical ALLCPR Course Performance" in html
    assert "Public Demand vs Actual Enrollment" in html
    assert "Scheduling Recommendation" in html
    assert "AHA BLS" in html
    # Phase 5: course strategy is now expressed through the opportunity graph's
    # recommendation strip rather than a separate "Best Course Strategy" section.
    assert "Course Opportunity Graph" in html
    assert "eval-rec" in html


def test_render_placeholder_when_no_data():
    html = _course_performance_section({})
    assert "Course Opportunity Graph" in html
    assert "No Enrollware class history loaded" in html


def test_aggregate_demand_counts_uses_max_not_sum():
    ranked = [
        ({"counts_5mi": {"hospital": 18}}, {}),
        ({"counts_5mi": {"hospital": 10}}, {}),
    ]
    assert aggregate_demand_counts(ranked)["hospital"] == 18


# --------------------------------------------------------------------------- #
# Excel path (only when openpyxl is installed)
# --------------------------------------------------------------------------- #

def test_load_records_from_excel(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Class Name", "Class Date", "Students Enrolled", "Max Students"])
    ws.append(["AHA BLS Provider", "2026-01-12", 9, 12])
    ws.append(["ARC Adult CPR/AED", "2026-02-02", 4, 12])
    path = tmp_path / "enrollware_classes.xlsx"
    wb.save(path)

    records = load_records(path)
    assert len(records) == 2
    assert {r.course_type for r in records} == {"aha_bls", "arc_cpr"}
    assert records[0].enrolled == 9


def test_default_file_discovery(monkeypatch, csv_file: Path):
    # When no path is given, the loader scans ENROLLWARE_FILES in order.
    monkeypatch.setattr(enrollware, "ENROLLWARE_FILES", [csv_file])
    records = load_records()
    assert len(records) == 9


def test_real_export_filenames_are_discoverable():
    class_names = {p.name for p in enrollware.ENROLLWARE_FILES}
    location_names = {p.name for p in enrollware.LOCATIONS_FILES}
    assert "Enrollware Data - Classes.xlsx" in class_names
    assert "Enrollware Data - Locations.xlsx" in location_names


def test_locations_export_resolves_city_state(tmp_path: Path):
    classes = tmp_path / "classes.csv"
    classes.write_text(
        "Class Name,Class Date,Students Enrolled,Location\n"
        "AHA BLS Provider,2026-01-12,9,SJ\n",
        encoding="utf-8",
    )
    locations = tmp_path / "locations.csv"
    locations.write_text(
        "Abbreviation,Name\n"
        "SJ,\"San Jose(1631 N First Street, Suite 200, San Jose, CA 95112)\"\n",
        encoding="utf-8",
    )

    records = load_records(classes, locations_path=locations)

    assert len(records) == 1
    assert records[0].city == "San Jose"
    assert records[0].state == "CA"
