"""Adzuna jobs API integration tests.

Mocks the HTTP layer to verify:
- response → posting-row mapping
- certification-keyword filter drops irrelevant postings
- feature-flag fallback when keys are unset
- CSV-precedence merge against the existing job_postings flow
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import adzuna_jobs, job_postings  # noqa: E402


# --------------------------------------------------------------------------- #
# Feature-flag fallback
# --------------------------------------------------------------------------- #

def test_adzuna_not_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_ID", "")
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_KEY", "")
    assert adzuna_jobs.is_configured() is False
    out = adzuna_jobs.fetch_adzuna_postings(
        city="San Francisco", state="CA",
        latitude=37.7749, longitude=-122.4194,
    )
    assert out == []


# --------------------------------------------------------------------------- #
# Response mapping + cert filtering
# --------------------------------------------------------------------------- #

def _fake_adzuna_result(
    *, title: str, description: str, employer: str = "Test Co",
    lat: float = 37.78, lon: float = -122.42, url: str = "https://ex/job/1",
) -> dict:
    return {
        "title": title,
        "description": description,
        "company": {"display_name": employer},
        "latitude": lat,
        "longitude": lon,
        "redirect_url": url,
        "created": "2026-05-15T12:00:00Z",
        "location": {"display_name": "San Francisco, CA"},
    }


def test_adzuna_keeps_cpr_required_posting(monkeypatch):
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_ID", "id-ok")
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_KEY", "key-ok")
    raw_results = [
        _fake_adzuna_result(
            title="Registered Nurse - ED",
            description="BLS required, AHA BLS within 30 days of hire.",
            employer="Mercy Hospital",
        ),
    ]
    with patch.object(adzuna_jobs, "_adzuna_request",
                      return_value=raw_results):
        rows = adzuna_jobs.fetch_adzuna_postings(
            city="San Francisco", state="CA",
            latitude=37.7749, longitude=-122.4194,
        )
    assert len(rows) == 1
    row = rows[0]
    assert row["employer"] == "Mercy Hospital"
    assert row["title"] == "Registered Nurse - ED"
    assert row["city"] == "San Francisco"
    assert row["state"] == "CA"
    assert row["radius_miles"] == "3"
    assert row["notes"].startswith("adzuna:")


def test_adzuna_drops_postings_without_certification_signal(monkeypatch):
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_ID", "id-ok")
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_KEY", "key-ok")
    raw_results = [
        _fake_adzuna_result(
            title="Software Engineer",
            description="Build microservices. No certification required.",
        ),
        _fake_adzuna_result(
            title="Janitor",
            description="Clean offices.",
        ),
    ]
    with patch.object(adzuna_jobs, "_adzuna_request",
                      return_value=raw_results):
        rows = adzuna_jobs.fetch_adzuna_postings(
            city="San Francisco", state="CA",
            latitude=37.7749, longitude=-122.4194,
        )
    assert rows == []


def test_adzuna_dedups_by_redirect_url(monkeypatch):
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_ID", "id-ok")
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_KEY", "key-ok")
    dupe = _fake_adzuna_result(
        title="EMT", description="BLS / CPR required.",
        url="https://ex/job/abc",
    )
    with patch.object(adzuna_jobs, "_adzuna_request",
                      return_value=[dupe, dupe]):
        rows = adzuna_jobs.fetch_adzuna_postings(
            city="SF", state="CA", latitude=37.77, longitude=-122.42,
        )
    assert len(rows) == 1


def test_adzuna_skips_results_without_coordinates(monkeypatch):
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_ID", "id-ok")
    monkeypatch.setattr(adzuna_jobs, "ADZUNA_APP_KEY", "key-ok")
    bad = _fake_adzuna_result(
        title="CNA", description="BLS required.",
        lat=None,  # type: ignore[arg-type]
        lon=None,  # type: ignore[arg-type]
    )
    with patch.object(adzuna_jobs, "_adzuna_request", return_value=[bad]):
        rows = adzuna_jobs.fetch_adzuna_postings(
            city="SF", state="CA", latitude=37.77, longitude=-122.42,
        )
    assert rows == []


# --------------------------------------------------------------------------- #
# CSV-precedence merge
# --------------------------------------------------------------------------- #

def test_merge_csv_precedence_when_source_url_overlaps():
    """Same source_url in both → CSV row wins, Adzuna copy dropped."""
    csv_rows = [{
        "source_url": "https://ex/job/abc",
        "city": "SF", "state": "CA", "latitude": "37.77", "longitude": "-122.42",
        "radius_miles": "2",
        "employer": "Manual Cited Hospital",
        "title": "RN", "description": "BLS required.",
        "posted_at": "2026-05-01", "notes": "csv",
    }]
    adzuna_rows = [{
        "source_url": "https://ex/job/abc",
        "city": "SF", "state": "CA", "latitude": "37.77", "longitude": "-122.42",
        "radius_miles": "0.5",
        "employer": "Adzuna Auto Hospital",
        "title": "RN", "description": "BLS required.",
        "posted_at": "2026-05-15", "notes": "adzuna",
    }]
    merged = job_postings._merge_csv_and_adzuna(csv_rows, adzuna_rows)
    assert len(merged) == 1
    assert merged[0]["employer"] == "Manual Cited Hospital"


def test_merge_appends_when_urls_differ():
    csv_rows = [{
        "source_url": "https://csv/job/1",
        "city": "SF", "state": "CA", "latitude": "37.77", "longitude": "-122.42",
        "radius_miles": "2", "employer": "From CSV", "title": "RN",
        "description": "BLS required.", "posted_at": "2026-05-01", "notes": "",
    }]
    adzuna_rows = [{
        "source_url": "https://adz/job/2",
        "city": "SF", "state": "CA", "latitude": "37.78", "longitude": "-122.43",
        "radius_miles": "0.5", "employer": "From Adzuna", "title": "EMT",
        "description": "BLS required.", "posted_at": "2026-05-10", "notes": "",
    }]
    merged = job_postings._merge_csv_and_adzuna(csv_rows, adzuna_rows)
    assert len(merged) == 2
    assert {r["employer"] for r in merged} == {"From CSV", "From Adzuna"}


def test_merge_returns_csv_when_adzuna_empty():
    csv_rows = [{
        "source_url": "https://csv/job/1", "employer": "Only",
        "city": "X", "state": "Y", "latitude": "1", "longitude": "1",
        "radius_miles": "1", "title": "T", "description": "BLS",
        "posted_at": "", "notes": "",
    }]
    merged = job_postings._merge_csv_and_adzuna(csv_rows, [])
    assert merged == csv_rows
