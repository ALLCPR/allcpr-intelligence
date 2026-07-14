"""API tests for /api/ops/*: shapes, CRM flow, rate limiting, leak guard."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import store
from app.ops.models import scrub_sensitive
from tests.ops_fixtures import (
    STRONG_ZIP_ROW,
    confirmed_instructor,
    confirmed_room,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with an isolated ops store and a deterministic ZIP row."""
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    monkeypatch.setattr("app.ops.routes._load_zip_row",
                        lambda zip_code: dict(STRONG_ZIP_ROW))
    monkeypatch.setattr("app.ops.routes.load_commercial_validation",
                        lambda: {})
    # Keep discovery deterministic: no real manual imports.
    monkeypatch.setattr("app.ops.instructor_supply.load_instructors_import",
                        lambda: [])
    monkeypatch.setattr("app.ops.space_supply.load_locations_import",
                        lambda: [])
    return TestClient(web_app.app)


def test_readiness_shape(client):
    resp = client.get("/api/ops/zip/95112/readiness")
    assert resp.status_code == 200
    data = resp.json()
    assert data["zip"] == "95112"
    for key in ("summary", "courses", "top_instructor_leads",
                "top_space_leads", "lead_counts", "last_updated_at"):
        assert key in data
    summary = data["summary"]
    for key in ("demand_label", "demand_score",
                "aha_instructor_readiness_label",
                "arc_instructor_readiness_label",
                "classroom_readiness_label", "operating_feasibility_score",
                "recommended_action", "recommended_action_label",
                "explanation", "missing_requirements", "risk_flags",
                "next_steps"):
        assert key in summary
    assert set(data["courses"]) == {"OVERALL", "AHA_BLS", "ARC_BLS",
                                    "ARC_CPR_FA_AED"}


def test_recommendation_shape(client):
    resp = client.get("/api/ops/zip/95112/recommendation")
    assert resp.status_code == 200
    data = resp.json()
    assert data["recommended_action"] in (
        "NOT_READY_DEMAND_WEAK", "NOT_READY_NO_INSTRUCTOR",
        "NOT_READY_NO_SPACE", "RESEARCH_NEEDED",
        "INSTRUCTOR_OUTREACH_NEEDED", "SPACE_OUTREACH_NEEDED",
        "TEST_CLASS_READY", "RECURRING_CLASS_CANDIDATE",
        "PERMANENT_CENTER_CANDIDATE")
    assert data["explanation"]
    assert isinstance(data["next_steps"], list)


def test_invalid_zip_rejected(client):
    assert client.get("/api/ops/zip/abcde/readiness").status_code == 400
    assert client.get("/api/ops/zip/1234/readiness").status_code == 400
    assert client.post("/api/ops/zip/12x45/refresh").status_code == 400


def test_lead_endpoints_and_discovery(client):
    inst = client.get("/api/ops/zip/95112/instructor-leads")
    assert inst.status_code == 200
    leads = inst.json()["leads"]
    assert leads, "strong ZIP row should yield signal leads"
    # Enrichment discovery must never claim verified credentials.
    assert all(l["credential_status"] == "SIGNAL_ONLY" for l in leads)
    spaces = client.get("/api/ops/zip/95112/space-leads")
    assert spaces.status_code == 200


def test_refresh_rate_limited_then_forced(client):
    first = client.post("/api/ops/zip/95112/refresh")
    assert first.status_code == 200
    second = client.post("/api/ops/zip/95112/refresh")
    assert second.status_code == 429
    forced = client.post("/api/ops/zip/95112/refresh?force=true")
    assert forced.status_code == 200


def test_crm_status_update_flow(client):
    lead = client.get("/api/ops/zip/95112/instructor-leads").json()["leads"][0]
    resp = client.post(
        f"/api/ops/leads/instructor/{lead['id']}/status",
        json={"status": "CONTACTED", "note": "Emailed program director"})
    assert resp.status_code == 200
    updated = resp.json()["lead"]
    assert updated["outreach_status"] == "CONTACTED"
    assert "Emailed program director" in updated["notes"]
    # Bad status rejected with the allowed list.
    bad = client.post(f"/api/ops/leads/instructor/{lead['id']}/status",
                      json={"status": "NOT_A_STATUS"})
    assert bad.status_code == 400
    # Unknown lead 404s.
    missing = client.post("/api/ops/leads/instructor/inst_nope/status",
                          json={"status": "CONTACTED"})
    assert missing.status_code == 404


def test_crm_state_survives_refresh(client):
    lead = client.get("/api/ops/zip/95112/instructor-leads").json()["leads"][0]
    client.post(f"/api/ops/leads/instructor/{lead['id']}/status",
                json={"status": "INTERESTED", "note": "wants weekend classes"})
    client.post("/api/ops/zip/95112/refresh?force=true")
    after = client.get("/api/ops/zip/95112/instructor-leads").json()["leads"]
    match = next(l for l in after if l["id"] == lead["id"])
    assert match["outreach_status"] == "INTERESTED"
    assert "wants weekend classes" in match["notes"]


def test_outreach_generate_creates_draft_and_log(client):
    lead = client.get("/api/ops/zip/95112/instructor-leads").json()["leads"][0]
    resp = client.post("/api/ops/outreach/generate",
                       json={"lead_type": "instructor", "lead_id": lead["id"],
                             "staff_name": "Alex Staff", "zip": "95112"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Instructor Opportunity" in data["draft"]["subject"]
    assert data["outreach_log_entry"]["status"] == "DRAFT"
    assert data["outreach_log_entry"]["sent_at"] is None
    # Unknown lead 404s; missing fields 400.
    assert client.post("/api/ops/outreach/generate",
                       json={"lead_type": "instructor",
                             "lead_id": "inst_nope"}).status_code == 404
    assert client.post("/api/ops/outreach/generate",
                       json={}).status_code == 400


# --------------------------------------------------------------------------
# Sensitive staff-only data must never leak
# --------------------------------------------------------------------------
SENSITIVE_VALUES = ("4321#", "hunter2-wifi", "LB-9876", "9-9-9-9")


def test_scrub_sensitive_drops_staff_only_keys():
    payload = {
        "name": "Room A",
        "access_code": SENSITIVE_VALUES[0],
        "wifi_password": SENSITIVE_VALUES[1],
        "lockbox_code": SENSITIVE_VALUES[2],
        "nested": {"door_code": SENSITIVE_VALUES[3], "capacity": 12},
        "leads": [{"alarm_code": "0000", "name": "ok"}],
    }
    clean = scrub_sensitive(payload)
    assert clean["name"] == "Room A"
    assert "access_code" not in clean
    assert "wifi_password" not in clean
    assert "lockbox_code" not in clean
    assert "door_code" not in clean["nested"]
    assert clean["nested"]["capacity"] == 12
    assert "alarm_code" not in clean["leads"][0]


def test_api_scrubs_sensitive_fields_from_stored_leads(client, tmp_path):
    # Simulate a manual edit that stuffed staff-only fields into a lead.
    tainted_inst = confirmed_instructor()
    tainted_inst["access_code"] = SENSITIVE_VALUES[0]
    tainted_inst["wifi_password"] = SENSITIVE_VALUES[1]
    tainted_space = confirmed_room()
    tainted_space["lockbox_code"] = SENSITIVE_VALUES[2]
    tainted_space["door_code"] = SENSITIVE_VALUES[3]
    store.save_zip_candidates("INSTRUCTOR", "95112", [tainted_inst])
    store.save_zip_candidates("SPACE", "95112", [tainted_space])

    for path in ("/api/ops/zip/95112/readiness",
                 "/api/ops/zip/95112/instructor-leads",
                 "/api/ops/zip/95112/space-leads",
                 "/api/ops/zip/95112/recommendation"):
        body = client.get(path).text
        for secret in SENSITIVE_VALUES:
            assert secret not in body, f"{path} leaked a staff-only value"
        for key in ("access_code", "wifi_password", "lockbox_code",
                    "door_code"):
            assert key not in body, f"{path} leaked key {key}"
