"""Tests for the BLS QCEW labor collector.

The collector parses the QCEW *CSV* endpoint (the .json variant was retired
and now 404s). Fixtures here serve CSV text via ``_FakeResp.text``.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

import pytest

from app.collectors import bls_or_labor


def _to_csv(rows):
    """Serialize a list of row-dicts to a QCEW-style CSV string."""
    if not rows:
        return ""
    fields = sorted({k for r in rows for k in r})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# A minimal QCEW area response with one healthcare row (NAICS 62 / private).
_FAKE_QCEW_ROWS = [
    {
        "area_fips": "06085",
        "own_code": "5",
        "industry_code": "10",   # noise
        "year": "2025",
        "qtr": "2",
        "month3_emplvl": "1000000",
    },
    {
        "area_fips": "06085",
        "own_code": "0",         # total covered, not private — should be skipped
        "industry_code": "62",
        "year": "2025",
        "qtr": "2",
        "month3_emplvl": "999",
    },
    {
        "area_fips": "06085",
        "own_code": "5",
        "industry_code": "62",   # Health Care and Social Assistance, private
        "year": "2025",
        "qtr": "2",
        "month3_emplvl": "85000",
        "lq_month3_emplvl": "1.18",
        "avg_wkly_wage": "1750",
    },
]

_FAKE_QCEW = _to_csv(_FAKE_QCEW_ROWS)


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        # payload may be a CSV string or a list of row-dicts (auto-serialized).
        if isinstance(payload, list):
            self.text = _to_csv(payload)
        else:
            self.text = payload if payload is not None else ""

    def json(self):  # retained for any legacy callers; not used by CSV path
        raise ValueError("CSV endpoint, not JSON")


# --- _candidate_quarters --------------------------------------------------- #

def test_candidate_quarters_recent_first():
    # As of mid-May 2026, two quarters back is 2025 Q4.
    quarters = bls_or_labor._candidate_quarters(today=date(2026, 5, 15))
    assert quarters[0] == (2025, 4)
    assert len(quarters) == bls_or_labor._MAX_QUARTERS_TO_TRY
    # Strictly descending in time.
    for prev, nxt in zip(quarters, quarters[1:]):
        assert (prev[0], prev[1]) > (nxt[0], nxt[1])


# --- successful path ------------------------------------------------------- #

def test_collect_labor_populates_healthcare_row(monkeypatch):
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: ("06", "085"))

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResp(200, _FAKE_QCEW)

    monkeypatch.setattr(bls_or_labor.requests, "get", fake_get)

    block = bls_or_labor.collect_labor(37.3213, -121.9478)
    values = block["values"]
    assert values["healthcare_employment_count"] == 85000
    assert values["healthcare_employment_lq"] == 1.18
    assert values["avg_weekly_wage_healthcare"] == 1750.0
    assert isinstance(values["data_year"], int)

    source = block["sources"][0]
    assert "BLS QCEW" in source["name"]
    assert "06085" in source["url"]
    populated = source["fields"]
    assert "healthcare_employment_count" in populated
    assert "healthcare_employment_lq" in populated
    assert "avg_weekly_wage_healthcare" in populated


# --- failure / fallback ---------------------------------------------------- #

def test_collect_labor_returns_stub_when_county_unresolved(monkeypatch):
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: None)
    block = bls_or_labor.collect_labor(37.0, -121.0)
    assert all(block["values"][k] is None for k in bls_or_labor.STUB_FIELDS)
    # Stub name must contain "stub" so confidence_score._trust returns 0.
    assert "stub" in block["sources"][0]["notes"].lower()
    assert "not yet integrated" in block["sources"][0]["name"].lower()


def test_collect_labor_falls_back_through_quarters(monkeypatch):
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: ("06", "085"))

    call_log = []

    def fake_get(url, **kwargs):
        call_log.append(url)
        # First two quarters not released yet (404), third has data.
        if len(call_log) <= 2:
            return _FakeResp(404)
        return _FakeResp(200, _FAKE_QCEW)

    monkeypatch.setattr(bls_or_labor.requests, "get", fake_get)

    block = bls_or_labor.collect_labor(37.3213, -121.9478)
    assert block["values"]["healthcare_employment_count"] == 85000
    assert len(call_log) == 3  # tried 2 misses, succeeded on 3rd


def test_collect_labor_stub_when_no_healthcare_row(monkeypatch):
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: ("06", "085"))
    # Response has data but no NAICS 62 private row at all.
    response = [{
        "industry_code": "11", "own_code": "5",
        "month3_emplvl": "100",
    }]
    monkeypatch.setattr(bls_or_labor.requests, "get",
                        lambda url, **kw: _FakeResp(200, response))
    block = bls_or_labor.collect_labor(37.0, -121.0)
    assert block["values"]["healthcare_employment_count"] is None
    assert "stub" in block["sources"][0]["notes"].lower()


def test_collect_labor_handles_request_exception(monkeypatch):
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: ("06", "085"))

    def boom(url, **kw):
        raise bls_or_labor.requests.RequestException("network down")

    monkeypatch.setattr(bls_or_labor.requests, "get", boom)
    # Also patch sleep so retries don't slow the test.
    monkeypatch.setattr("time.sleep", lambda *_: None)

    block = bls_or_labor.collect_labor(37.0, -121.0)
    assert block["values"]["healthcare_employment_count"] is None
    assert "stub" in block["sources"][0]["notes"].lower()


# --- accepts caller-supplied FIPS ----------------------------------------- #

def test_collect_labor_uses_supplied_fips_without_geocoding(monkeypatch):
    # If a caller already knows the FIPS, the geocoder is not consulted.
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: pytest.fail("geocoder should not be called"))
    monkeypatch.setattr(bls_or_labor.requests, "get",
                        lambda url, **kw: _FakeResp(200, _FAKE_QCEW))
    block = bls_or_labor.collect_labor(37.0, -121.0,
                                       state_fips="06", county_fips="085")
    assert block["values"]["healthcare_employment_count"] == 85000
