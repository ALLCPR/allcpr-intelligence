"""Tests for the manual commercial-validation layer."""
from __future__ import annotations

from app.reports import commercial_validation as cv

CSV = (
    "zip,address,property_name,sqft,monthly_rent,parking,available,classroom_fit,source_url,broker_contact,notes,updated_at\n"
    "95112,123 A St,Plaza,900,\"$3,200\",Yes,Yes,Good,https://x.com/1,Broker A,Good parking,2026-06-11\n"
    "95112,455 B Ave,Suites,650,2600,Mixed,No,Possible,https://x.com/2,Agent B,Leased,2026-05-20\n"
    "BAD,nope,,,,,,,,,,\n"            # bad ZIP -> skipped
    "97202,88 C Rd,Center,1100,3800,No,Yes,Limited,,,No lot,2026-06-01\n"
)


def _write(tmp_path):
    p = tmp_path / "commercial_validation.csv"
    p.write_text(CSV, encoding="utf-8")
    return p


def test_missing_file_returns_empty(tmp_path):
    assert cv.load_commercial_validation(tmp_path / "nope.csv") == {}
    assert cv.load_commercial_summaries(tmp_path / "nope.csv") == {}


def test_loads_grouped_by_zip_and_skips_bad_rows(tmp_path):
    grouped = cv.load_commercial_validation(_write(tmp_path))
    assert set(grouped) == {"95112", "97202"}     # BAD row dropped
    assert len(grouped["95112"]) == 2


def test_rent_parsing_and_min_max_avg(tmp_path):
    summaries = cv.load_commercial_summaries(_write(tmp_path))
    s = summaries["95112"]
    assert s["rent_min"] == 2600
    assert s["rent_max"] == 3200
    assert s["rent_avg"] == 2900     # ($3,200 parsed despite symbol/comma)


def test_summary_fields_and_ready_flag(tmp_path):
    summaries = cv.load_commercial_summaries(_write(tmp_path))
    s = summaries["95112"]
    assert s["commercial_validated"] is True
    assert s["commercial_space_count"] == 2
    assert s["available_space_count"] == 1
    assert s["parking_summary"] == "Mixed"
    assert s["classroom_fit_summary"] == "Good"
    assert s["commercial_ready"] is True          # available + parking + good fit
    assert "https://x.com/1" in s["commercial_sources"]
    assert s["commercial_updated_at"] == "2026-06-11"


def test_not_ready_when_no_parking(tmp_path):
    # 97202: available + good-ish but parking "No" -> not ready.
    s = cv.load_commercial_summaries(_write(tmp_path))["97202"]
    assert s["available_space_count"] == 1
    assert s["parking_summary"] == "No"
    assert s["commercial_ready"] is False


def test_empty_rows_summary():
    assert cv.summarize_commercial_validation([]) == {"commercial_validated": False}
