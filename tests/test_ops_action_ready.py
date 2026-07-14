"""Action-ready upgrades: greeting honesty, lead stages, checklist,
signals-only notice, named/signal lead separation, search queries."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import store
from app.ops.instructor_supply import instructor_readiness_score
from app.ops.models import is_sensitive_key, is_signal_lead
from app.ops.operating_feasibility import (
    action_checklist,
    build_zip_operating_readiness,
    compute_course_readiness,
)
from app.ops.outreach_templates import (
    generate_instructor_outreach,
    generate_space_outreach,
)
from app.ops.search_queries import generate_search_queries
from app.ops.space_supply import classroom_readiness_score
from tests.ops_fixtures import (
    STRONG_ZIP_ROW,
    confirmed_instructor,
    confirmed_room,
    named_instructor_lead,
    signal_instructor_lead,
    signal_room,
)


# --------------------------------------------------------------------------
# 1. Greeting honesty: category/signal leads are never greeted by category
# --------------------------------------------------------------------------
def test_signal_instructor_lead_not_used_as_greeting():
    lead = signal_instructor_lead()
    draft = generate_instructor_outreach(lead, zip_code="95112")
    first_line = draft["body"].splitlines()[0]
    assert first_line == "Hi [Program Coordinator],"
    assert "Nursing program faculty" not in first_line


def test_signal_room_lead_not_used_as_greeting():
    room = signal_room()
    draft = generate_space_outreach(room, zip_code="95112")
    first_line = draft["body"].splitlines()[0]
    assert first_line == "Hi [Facility Manager],"
    assert "Community centers" not in first_line


def test_named_lead_still_greeted_by_name():
    draft = generate_instructor_outreach(named_instructor_lead(),
                                         zip_code="95112")
    assert draft["body"].startswith("Hi Nursing Faculty Lead,")


def test_empty_name_gets_placeholder():
    lead = named_instructor_lead(name="")
    draft = generate_instructor_outreach(lead, zip_code="95112")
    assert draft["body"].startswith("Hi [Name],")


# --------------------------------------------------------------------------
# 2. Lead stages: Signal Only shown when no named candidate exists
# --------------------------------------------------------------------------
def test_instructor_stage_signal_only_without_named_candidate():
    readiness = instructor_readiness_score(
        [signal_instructor_lead(), signal_instructor_lead()])
    assert readiness["stage"] == "Signal Only"
    assert readiness["score"] == 50.0  # score unchanged — wording is honest


def test_instructor_stage_candidate_found_and_confirmed():
    assert instructor_readiness_score(
        [named_instructor_lead()])["stage"] == "Candidate Found"
    assert instructor_readiness_score(
        [confirmed_instructor()])["stage"] == "Confirmed"


def test_instructor_stage_no_signal_when_empty():
    assert instructor_readiness_score([])["stage"] == "No Signal"


def test_classroom_stage_signal_only_and_candidate_found():
    assert classroom_readiness_score([signal_room()])["stage"] == "Signal Only"
    assert classroom_readiness_score(
        [confirmed_room()])["stage"] == "Confirmed"
    assert classroom_readiness_score([])["stage"] == "No Signal"


def test_classroom_stage_blocked_when_all_rooms_eliminated():
    bad = confirmed_room(outreach_status="NEW")
    bad["hard_elimination_flags"] = ["training_use_not_allowed"]
    assert classroom_readiness_score([bad])["stage"] == "Blocked"


# --------------------------------------------------------------------------
# 3. Action checklist
# --------------------------------------------------------------------------
def _checklist_map(items):
    return {item["key"]: item["done"] for item in items}


def test_checklist_flags_missing_named_instructor_and_room():
    done = _checklist_map(action_checklist(
        [signal_instructor_lead()], [signal_room()]))
    assert done["named_instructor_candidate"] is False
    assert done["specific_room_candidate"] is False
    assert done["verified_instructor_credential"] is False
    assert done["room_price_confirmed"] is False


def test_checklist_checks_off_confirmed_facts():
    inst = confirmed_instructor(rate_notes="$60/hr",
                                availability_notes="Weekends")
    room = confirmed_room(hourly_rate=40.0)
    done = _checklist_map(action_checklist([inst], [room]))
    assert done["named_instructor_candidate"] is True
    assert done["verified_instructor_credential"] is True
    assert done["instructor_rate_confirmed"] is True
    assert done["instructor_availability_confirmed"] is True
    assert done["specific_room_candidate"] is True
    assert done["room_price_confirmed"] is True
    assert done["weekend_evening_access_confirmed"] is True
    assert done["training_use_allowed"] is True
    assert done["recurring_booking_possible"] is True


def test_checklist_signals_never_satisfy_items():
    # A signal lead with notes must not check off instructor facts.
    sig = signal_instructor_lead(rate_notes="whatever",
                                 availability_notes="whatever")
    done = _checklist_map(action_checklist([sig], []))
    assert done["instructor_rate_confirmed"] is False
    assert done["instructor_availability_confirmed"] is False


# --------------------------------------------------------------------------
# 4. Signals-only notice
# --------------------------------------------------------------------------
def test_not_ready_notice_when_signals_only():
    record = compute_course_readiness(
        "95112", STRONG_ZIP_ROW,
        [signal_instructor_lead()], [signal_room()])
    assert record["signals_only"] is True
    assert "institutional signals only" in record["not_ready_notice"]


def test_no_notice_when_instructor_and_room_are_real():
    record = compute_course_readiness(
        "95112", STRONG_ZIP_ROW,
        [confirmed_instructor()], [confirmed_room()])
    assert record["signals_only"] is False
    assert record["not_ready_notice"] == ""


# --------------------------------------------------------------------------
# 5/6. Named leads separated from institutional signals
# --------------------------------------------------------------------------
def test_build_separates_named_and_signal_leads():
    payload = build_zip_operating_readiness(
        "95112", STRONG_ZIP_ROW,
        [named_instructor_lead(), signal_instructor_lead()],
        [confirmed_room(), signal_room()])
    assert all(not is_signal_lead(l)
               for l in payload["named_instructor_leads"])
    assert all(is_signal_lead(l)
               for l in payload["instructor_signal_leads"])
    assert all(not is_signal_lead(l) for l in payload["named_space_leads"])
    assert all(is_signal_lead(l) for l in payload["space_signal_leads"])
    assert payload["named_instructor_leads"]
    assert payload["instructor_signal_leads"]
    # Backward-compatible mixed lists still present.
    assert "top_instructor_leads" in payload
    assert "top_space_leads" in payload
    counts = payload["lead_counts"]
    assert counts["named_instructors"] == 1
    assert counts["named_spaces"] == 1
    summary = payload["summary"]
    for key in ("aha_instructor_readiness_stage",
                "arc_instructor_readiness_stage",
                "classroom_readiness_stage", "action_checklist",
                "signals_only", "not_ready_notice"):
        assert key in summary


# --------------------------------------------------------------------------
# 7. Search queries (manual lead sourcing bridge)
# --------------------------------------------------------------------------
def test_search_queries_use_city_when_given():
    data = generate_search_queries("94541", city="Hayward", state="CA")
    assert data["place_used"] == "Hayward CA"
    assert data["instructor_queries"] and data["space_queries"]
    combined = " ".join(q["query"] for q in
                        data["instructor_queries"] + data["space_queries"])
    assert "Hayward CA" in combined
    assert any("AHA BLS Instructor" in q["query"]
               for q in data["instructor_queries"])
    assert any("meeting room rental" in q["query"]
               for q in data["space_queries"])
    assert all(q["url"].startswith("https://www.google.com/search?q=")
               for q in data["instructor_queries"])
    assert data["linkedin_people_search"]["url"].startswith(
        "https://www.linkedin.com/")


def test_search_queries_fall_back_to_zip():
    data = generate_search_queries("94541")
    assert data["place_used"] == "94541"
    assert "94541" in data["instructor_queries"][0]["query"]


def test_search_queries_have_no_sensitive_keys():
    data = generate_search_queries("94541", city="Hayward")

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert not is_sensitive_key(k)
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(data)


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    monkeypatch.setattr("app.ops.routes._load_zip_row",
                        lambda zip_code: dict(STRONG_ZIP_ROW))
    monkeypatch.setattr("app.ops.routes.load_commercial_validation",
                        lambda: {})
    monkeypatch.setattr("app.ops.instructor_supply.load_instructors_import",
                        lambda: [])
    monkeypatch.setattr("app.ops.space_supply.load_locations_import",
                        lambda: [])
    return TestClient(web_app.app)


def test_search_queries_endpoint(client):
    resp = client.get("/api/ops/zip/95112/search-queries?city=San%20Jose")
    assert resp.status_code == 200
    data = resp.json()
    assert data["place_used"] == "San Jose"
    assert data["instructor_queries"] and data["space_queries"]
    assert client.get("/api/ops/zip/12ab5/search-queries").status_code == 400


def test_readiness_endpoint_separates_leads_and_flags_signals_only(client):
    resp = client.get("/api/ops/zip/95112/readiness")
    assert resp.status_code == 200
    data = resp.json()
    # Discovery on a signals-only ZIP: no named leads, only signals.
    assert data["named_instructor_leads"] == []
    assert data["instructor_signal_leads"]
    assert data["summary"]["signals_only"] is True
    assert "institutional signals only" in data["summary"]["not_ready_notice"]
    checklist = data["summary"]["action_checklist"]
    assert checklist and all(item["done"] is False for item in checklist)
