"""Data-quality handling in the Enrollware loader: blank rows, location
normalization, duplicate/ambiguous abbreviations, missing fields, capacity
anomalies, and the summary surfaced to logs + report."""
from __future__ import annotations

from pathlib import Path

from app.collectors.enrollware import (
    _normalize_location_name,
    load_enrollware,
    load_records,
)

CLASSES_HEADER = ("Course,Start Date / Time,End Date / Time,Location,"
                  "Students,Seats,Hours")
LOCATIONS_HEADER = "Abbreviation,Name"


def _write(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _classes(tmp_path: Path, rows: list[str]) -> Path:
    return _write(tmp_path / "classes.csv", CLASSES_HEADER, rows)


def _locations(tmp_path: Path, rows: list[str]) -> Path:
    return _write(tmp_path / "locations.csv", LOCATIONS_HEADER, rows)


def test_location_name_normalization():
    assert _normalize_location_name("San Jose (t)") == "San Jose"
    assert _normalize_location_name("Durham (tmp)") == "Durham"
    assert _normalize_location_name("group training (02/12/2026)") == "group training"
    assert _normalize_location_name("  ") is None
    assert _normalize_location_name(None) is None


def test_blank_rows_ignored(tmp_path: Path):
    rows = [
        "ARC BLS,2025-01-05,2025-01-05,San Jose,6,12,2",
        ",,,,,,",          # fully blank formatted row
        ",,,,,,",
        "ARC CPR,2025-02-05,2025-02-05,San Jose,4,12,2",
    ]
    _, dq = load_enrollware(_classes(tmp_path, rows))
    assert dq.classes_loaded == 2
    assert dq.classes_blank_ignored == 2
    assert dq.classes_total_rows == 4


def test_location_normalized_before_join(tmp_path: Path):
    cls = _classes(tmp_path, [
        "ARC BLS,2025-01-05,2025-01-05,San Jose (t),6,12,2",
    ])
    loc = _locations(tmp_path, [
        'San Jose,"San Jose(123 First St, San Jose, CA 95112)"',
    ])
    recs, _ = load_enrollware(cls, loc)
    assert recs[0].location == "San Jose"       # normalized
    assert recs[0].city == "San Jose"           # joined despite the "(t)"
    assert recs[0].state == "CA"


def test_ambiguous_abbreviation_not_force_resolved(tmp_path: Path):
    cls = _classes(tmp_path, [
        "ARC BLS,2025-01-05,2025-01-05,Troy,6,12,2",
    ])
    loc = _locations(tmp_path, [
        'Troy,"Troy(1 A St, Troy, MI 48083)"',
        'Troy,"Troy(2 B St, Troy, NY 12180)"',   # same code, different city
    ])
    recs, dq = load_enrollware(cls, loc)
    assert recs[0].city is None                 # we do NOT guess a city
    assert dq.ambiguous_location_rows == 1
    assert "Troy" in dq.ambiguous_abbreviations
    assert dq.duplicate_abbreviations.get("Troy") == 2


def test_duplicate_same_city_is_not_ambiguous(tmp_path: Path):
    cls = _classes(tmp_path, [
        "ARC BLS,2025-01-05,2025-01-05,Plano,6,12,2",
    ])
    loc = _locations(tmp_path, [
        'Plano,"Plano(1 A St, Plano, TX 75024)"',
        'Plano,"Plano(2 B St, Plano, TX 75024)"',
    ])
    recs, dq = load_enrollware(cls, loc)
    assert dq.duplicate_abbreviations.get("Plano") == 2
    assert "Plano" not in dq.ambiguous_abbreviations
    assert recs[0].city == "Plano"              # unambiguous -> resolved


def test_missing_fields_counted_safely(tmp_path: Path):
    rows = [
        "ARC BLS,2025-01-05,2025-01-05,San Jose,6,12,2",
        "ARC CPR,2025-02-05,,,4,12,",   # missing end date, location, hours
    ]
    recs, dq = load_enrollware(_classes(tmp_path, rows))
    assert dq.classes_loaded == 2       # neither row rejected
    assert dq.missing_location == 1
    assert dq.missing_end_date == 1
    assert dq.missing_hours == 1


def test_capacity_anomalies_flagged_not_fatal(tmp_path: Path):
    rows = [
        "ARC BLS,2025-01-05,2025-01-05,San Jose,13,12,2",  # overfilled
        "ARC CPR,2025-02-05,2025-02-05,San Jose,5,0,2",    # seats=0, students>0
        "AHA BLS,2025-03-05,2025-03-05,San Jose,0,12,2",   # zero students
    ]
    recs, dq = load_enrollware(_classes(tmp_path, rows))
    assert dq.classes_loaded == 3
    assert dq.capacity_overfilled == 1
    assert dq.zero_seats_with_students == 1
    assert dq.zero_student_rows == 1


def test_zero_students_excluded_from_held_kept_in_total(tmp_path: Path):
    rows = [
        "ARC BLS,2025-01-05,2025-01-05,San Jose,6,12,2",
        "ARC BLS,2025-02-05,2025-02-05,San Jose,0,12,2",   # cancelled/no-show
    ]
    _, dq = load_enrollware(_classes(tmp_path, rows))
    assert dq.classes_loaded == 2       # both kept for risk analysis
    assert dq.held_classes == 1         # only the real one counts for averages


def test_load_records_backcompat_returns_list(tmp_path: Path):
    rows = ["ARC BLS,2025-01-05,2025-01-05,San Jose,6,12,2"]
    recs = load_records(_classes(tmp_path, rows))
    assert isinstance(recs, list) and len(recs) == 1
