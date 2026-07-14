"""
Tests for the Enrollware → ops-layer manual import converter and the
local-market context it feeds.

Covers the pure transforms (no spreadsheet needed), the email/phone plumbing
into instructor candidates, competitor-pricing + demand aggregation, and the
no-PII-leak guarantee at the API scrub boundary.
"""
from __future__ import annotations

import csv
from pathlib import Path

from app.ops import local_market
from app.ops.imports import load_instructors_import
from app.ops.instructor_supply import _roster_candidate
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED, scrub_sensitive
from scripts import import_enrollware_ops_data as conv


# --------------------------------------------------------------------------
# Instructor record transform
# --------------------------------------------------------------------------
def test_instructor_record_active_row_maps_all_fields():
    raw = {
        "Active": "Yes", "First Name": "AI", "Last Name": "He",
        "Email": "alhe@example.org", "Phone": "5551234567",
        "City": "San Jose", "State": "CA", "Zip": "95112",
        "AHA ID": "24123189701.0", "HSI ID": "U083F5TDLG5",
        "Certifications": "ARC BLS\nARC CPR",
        "Certifications - Expiration Dates": "1/1/2026\n2/2/2026",
        "Notes": "#Salary:25\n#Payment Method:QB-ACH\n#Employment Type:W-2",
    }
    rec = conv.instructor_record(raw)
    assert rec is not None
    assert rec["name"] == "AI He"
    assert rec["email"] == "alhe@example.org"
    assert rec["phone"] == "5551234567"
    assert rec["zip"] == "95112"
    assert rec["pay_rate"] == "$25"
    # ARC cert text + both ids → all three teachable courses.
    assert AHA_BLS in rec["courses"]
    assert ARC_BLS in rec["courses"]
    # Honesty rule: a roster row is never pre-verified.
    assert rec["verified"] == "no"
    # Raw id numbers must not leak into the served certifications cell.
    assert "24123189701" not in rec["certifications"]
    assert "AHA instructor ID on file" in rec["certifications"]


def test_instructor_record_skips_inactive_and_nameless():
    assert conv.instructor_record({"Active": "No", "First Name": "X",
                                   "Last Name": "Y"}) is None
    assert conv.instructor_record({"Active": "Yes", "First Name": "",
                                   "Last Name": ""}) is None


def test_instructor_record_flags_cert_renewal_list():
    raw = {"Active": "Yes", "First Name": "Miles", "Last Name": "Aguiar",
           "Email": "milo@example.com"}
    rec = conv.instructor_record(raw, missing_cert_emails={"milo@example.com"})
    assert "cert-renewal" in rec["reliability_notes"].lower()


def test_parse_salary_variants():
    assert conv.parse_salary("#Salary:45\n#Payment Method:R") == "$45"
    assert conv.parse_salary("Salary = $60/hr") == "$60"
    assert conv.parse_salary("no salary here") == ""
    assert conv.parse_salary(None) == ""


def test_derive_courses_from_ids_and_text():
    assert conv.derive_courses("12345.0", "", "") == [AHA_BLS]
    hsi = conv.derive_courses("", "HSIID", "")
    assert ARC_BLS in hsi and ARC_CPR_FA_AED in hsi and AHA_BLS not in hsi
    assert conv.derive_courses("0.0", "0", "") == []  # blank/zero ids → none


# --------------------------------------------------------------------------
# Location transform
# --------------------------------------------------------------------------
def test_parse_location_name_extracts_address_and_zip():
    label, addr, zip_code = conv.parse_location_name(
        "Albany (124 Washington Avenue Extension, Albany, NY 12203)")
    assert label == "Albany"
    assert addr == "124 Washington Avenue Extension, Albany, NY 12203"
    assert zip_code == "12203"


def test_parse_location_name_handles_no_parens():
    label, addr, zip_code = conv.parse_location_name("Mobile Unit")
    assert label == "Mobile Unit"
    assert addr == "" and zip_code == ""


def test_location_record_imports_as_active():
    rec = conv.location_record(
        {"Name": "Atlanta (337 Georgia Ave se, Atlanta, GA 30312)",
         "Abbreviation": "Atlanta", "Directions": "Park in rear lot."})
    assert rec["location_name"] == "Atlanta"
    assert rec["zip"] == "30312"
    assert rec["active_status"] == "active"
    assert "rear lot" in rec["room_notes"]


# --------------------------------------------------------------------------
# Competitor aggregation
# --------------------------------------------------------------------------
def test_aggregate_competitor_classes_groups_and_prices():
    rows = [
        {"venue_zipcode": "95112", "course_type": "BLS", "price": "70",
         "provider": "Red Cross", "location": "San Jose, CA"},
        {"venue_zipcode": "95112", "course_type": "BLS", "price": "90",
         "provider": "Vital Connect", "location": "San Jose, CA"},
        {"venue_zipcode": "95112", "course_type": "CPR", "price": "75",
         "provider": "Red Cross", "location": "San Jose, CA"},
        {"venue_zipcode": "", "course_type": "BLS", "price": "50",
         "provider": "Ignored - no zip", "location": "x"},
    ]
    out = conv.aggregate_competitor_classes(rows)
    bls = next(r for r in out if r["course_type"] == "BLS")
    assert bls["zip"] == "95112"
    assert bls["class_count"] == "2"
    assert bls["provider_count"] == "2"
    assert bls["median_price"] == "80"  # median(70, 90)
    assert bls["min_price"] == "70" and bls["max_price"] == "90"
    # A row with no venue ZIP is dropped, not mis-bucketed.
    assert all("Ignored" not in r["providers"] for r in out)


# --------------------------------------------------------------------------
# Demand aggregation
# --------------------------------------------------------------------------
def test_date_sort_key_beats_lexical_comparison():
    # "7/7/26" is later than "12/1/25" chronologically but earlier lexically.
    assert conv._date_sort_key("7/7/26 7:57 AM") > conv._date_sort_key(
        "12/1/25 9:00 AM")
    assert conv._date_sort_key("2026-07-07") == (2026, 7, 7)
    assert conv._date_sort_key("garbage") == (0, 0, 0)


def test_aggregate_local_demand_counts_and_latest():
    rows = [
        {"Mailing Zip": "95112", "Course": "ARC BLS", "Class ID": "1",
         "Instructor": "A", "Reg. Date": "12/1/25 9:00 AM"},
        {"Mailing Zip": "95112", "Course": "ARC BLS", "Class ID": "2",
         "Instructor": "B", "Reg. Date": "7/7/26 8:00 AM"},
        {"Mailing Zip": "95050", "Course": "CPR", "Class ID": "3",
         "Instructor": "A", "Reg. Date": "1/1/26"},
    ]
    out = conv.aggregate_local_demand(rows)
    top = next(r for r in out if r["zip"] == "95112")
    assert top["student_count"] == "2"
    assert top["class_count"] == "2"
    assert top["instructor_count"] == "2"
    assert top["latest_registration"] == "7/7/26"  # chronological max
    assert "ARC BLS (2)" in top["top_courses"]
    # Results sorted by student_count desc.
    assert out[0]["zip"] == "95112"


# --------------------------------------------------------------------------
# Email/phone plumbing into candidates + importer
# --------------------------------------------------------------------------
def test_roster_candidate_carries_email_and_phone():
    row = {"name": "AI He", "email": "alhe@example.org", "phone": "5551234567",
           "city": "San Jose", "state": "CA", "zip": "95112",
           "courses": ["AHA_BLS"], "certifications": ["AHA BLS"],
           "long_term_interest": "UNKNOWN", "verified": False}
    cand = _roster_candidate(row, "95112")
    assert cand["email"] == "alhe@example.org"
    assert cand["phone"] == "5551234567"


def test_importer_parses_email_and_phone(tmp_path: Path):
    p = tmp_path / "allcpr_instructors.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "email", "phone", "zip", "courses", "verified"])
        w.writerow(["Jane Doe", "jane@example.com", "5550001111", "95112",
                    "AHA_BLS", "no"])
    rows = load_instructors_import(p)
    assert rows[0]["email"] == "jane@example.com"
    assert rows[0]["phone"] == "5550001111"


# --------------------------------------------------------------------------
# Local-market context reading
# --------------------------------------------------------------------------
def _write(path: Path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_competitor_context_reads_and_filters(tmp_path: Path):
    p = tmp_path / "competitor_classes.csv"
    _write(p, conv.COMPETITOR_COLUMNS, [
        {"zip": "95112", "course_type": "BLS", "class_count": "4",
         "provider_count": "2", "median_price": "80", "min_price": "70",
         "max_price": "95", "providers": "Red Cross; Vital Connect",
         "sample_locations": "San Jose, CA"},
        {"zip": "99999", "course_type": "CPR", "class_count": "1",
         "provider_count": "1", "median_price": "60", "min_price": "60",
         "max_price": "60", "providers": "Other", "sample_locations": "x"},
    ])
    ctx = local_market.competitor_context("95112", path=p)
    assert ctx["has_data"] is True
    assert ctx["total_classes"] == 4
    assert ctx["courses"][0]["median_price"] == 80.0
    assert "Red Cross" in ctx["providers"]
    # Padding: a short ZIP still matches its zero-padded row.
    assert local_market.competitor_context("00000", path=p)["has_data"] is False


def test_local_demand_context_reads(tmp_path: Path):
    p = tmp_path / "local_demand.csv"
    _write(p, conv.DEMAND_COLUMNS, [
        {"zip": "95112", "student_count": "42", "class_count": "18",
         "top_courses": "ARC BLS (24); AHA BLS (6)", "instructor_count": "5",
         "latest_registration": "2026-06-28"},
    ])
    ctx = local_market.local_demand_context("95112", path=p)
    assert ctx["student_count"] == 42
    assert ctx["instructor_count"] == 5
    assert ctx["top_courses"][0].startswith("ARC BLS")
    assert local_market.local_demand_context("00001", path=p)["has_data"] is False


def test_missing_local_market_files_never_crash(tmp_path: Path):
    missing = tmp_path / "nope.csv"
    assert local_market.competitor_context("95112", path=missing)["has_data"] is False
    assert local_market.local_demand_context("95112", path=missing)["has_data"] is False


# --------------------------------------------------------------------------
# No PII / sensitive leak
# --------------------------------------------------------------------------
def test_local_market_payload_has_no_sensitive_keys(tmp_path: Path):
    # Defense in depth: the context builder allowlists its output keys, so a
    # stray operational-secret column in the CSV never reaches the payload;
    # scrub_sensitive at the API edge is the second line of defense.
    comp = tmp_path / "competitor_classes.csv"
    _write(comp, conv.COMPETITOR_COLUMNS + ["door_code"], [
        {"zip": "95112", "course_type": "BLS", "class_count": "1",
         "provider_count": "1", "median_price": "80", "min_price": "80",
         "max_price": "80", "providers": "Red Cross", "sample_locations": "x",
         "door_code": "1234"},
    ])
    ctx = local_market.competitor_context("95112", path=comp)
    scrubbed = scrub_sensitive({"local_market": {"competitor": ctx}})
    flat = str(scrubbed)
    assert "door_code" not in flat
    assert "1234" not in flat
    assert scrubbed["local_market"]["competitor"]["total_classes"] == 1
