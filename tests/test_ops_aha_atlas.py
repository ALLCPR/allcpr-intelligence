"""AHA Atlas live-lead source: mapping, filtering, persistence, draft pipeline.

No real network calls — ``fetch_training_centers`` is monkeypatched with a
canned Atlas response so the mapping/filtering rules and the end-to-end route
pipeline (fetch → persist → match → draft-all) are exercised deterministically.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import aha_atlas, store
from app.ops.models import AHA_BLS


# A canned AHA orgSearch item set covering every filter branch.
RAW_ITEMS = [
    {  # keep: no website, real phone + email, in radius
        "name": "Beat CPR Training", "code": "CA21011", "organisationType": "TC",
        "phone": "(408) 345-3588", "email": None, "websiteUrl": None,
        "distance": 6276.0,  # ~3.9 mi
        "disciplines": [{"name": "Basic Life Support", "code": "BLS"},
                        {"name": "Heartsaver", "code": "HS"}],
        "organisationProfile": {
            "address1": "1 Main St", "city": "San Jose", "state": "California",
            "postalCode": "95112", "country": "United States",
            "latitude": 37.35, "longitude": -121.9,
            "coordinator": {"email": "patty@beatcpr.com"}},
    },
    {  # drop: has its own website
        "name": "Has Website Inc", "code": "TS0001", "organisationType": "TS",
        "phone": "408-111-2222", "email": "a@b.com",
        "websiteUrl": "https://haswebsite.com", "distance": 1000.0,
        "disciplines": [{"name": "Basic Life Support", "code": "BLS"}],
        "organisationProfile": {"city": "San Jose", "state": "California",
                                "postalCode": "95112",
                                "coordinator": {"email": None}},
    },
    {  # drop: .org institutional email
        "name": "Nonprofit Health Org", "code": "TS0002", "organisationType": "TS",
        "phone": "", "email": "contact@bignonprofit.org", "websiteUrl": None,
        "distance": 2000.0, "disciplines": [],
        "organisationProfile": {"city": "San Jose", "state": "California",
                                "postalCode": "95112",
                                "coordinator": {"email": None}},
    },
    {  # drop: no contact at all (placeholder phone, no email)
        "name": "No Contact Site", "code": "TS0003", "organisationType": "TS",
        "phone": "0000000000", "email": None, "websiteUrl": None,
        "distance": 3000.0, "disciplines": [],
        "organisationProfile": {"city": "San Jose", "state": "California",
                                "coordinator": {"email": None}},
    },
    {  # drop: outside the requested radius (~40 mi)
        "name": "Too Far Away", "code": "TS0004", "organisationType": "TS",
        "phone": "408-999-0000", "email": None, "websiteUrl": None,
        "distance": 64000.0, "disciplines": [],
        "organisationProfile": {"city": "Gilroy", "state": "California",
                                "coordinator": {"email": "far@site.com"}},
    },
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    monkeypatch.setattr("app.ops.routes._load_zip_row", lambda zip_code: {})
    monkeypatch.setattr("app.ops.instructor_supply.load_instructors_import",
                        lambda: [])
    monkeypatch.setattr("app.ops.space_supply.load_locations_import",
                        lambda: [])
    monkeypatch.setattr("app.ops.routes.load_commercial_validation",
                        lambda: {})
    # Network-free AHA source + no cross-test cache bleed.
    aha_atlas._CACHE.clear()
    monkeypatch.setattr(aha_atlas, "fetch_training_centers",
                        lambda zip_code, radius_miles=25.0: list(RAW_ITEMS))
    monkeypatch.setenv("OUTREACH_STAFF_NAME", "Leon")
    return TestClient(web_app.app)


def test_mapping_and_filters_keep_only_recruiting_leads():
    result = aha_atlas._build_result("95112", 25.0,
                                     [aha_atlas.extract_center(i) for i in RAW_ITEMS],
                                     limit=25, cached=False)
    assert result["fetched"] == 5
    assert result["kept"] == 1  # only Beat CPR survives every filter
    cand = result["candidates"][0]
    assert cand["name"] == "Beat CPR Training"
    assert cand["source"] == "aha_atlas"
    assert cand["id"] == "aha_CA21011"  # stable id from AHA code
    assert cand["credential_status"] == "NEEDS_VERIFICATION"  # never VERIFIED
    assert cand["courses_possible"] == [AHA_BLS]
    assert cand["email"] == "patty@beatcpr.com"  # coordinator email fallback
    assert cand["phone"] == "(408) 345-3588"
    assert cand["distance_miles"] == pytest.approx(3.9, abs=0.1)


def test_placeholder_phone_is_blanked():
    center = aha_atlas.extract_center(RAW_ITEMS[3])
    assert center["phone"] == ""  # 0000000000 → blank


def test_get_route_persists_leads_into_store(client):
    resp = client.get("/api/ops/zip/95112/aha-instructors?radius=25")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["added"] == 1
    assert data["count"] == 1
    assert data["leads"][0]["source"] == "aha_atlas"
    # Persisted: a second call merges (does not duplicate) the stable id.
    resp2 = client.get("/api/ops/zip/95112/aha-instructors?radius=25")
    assert resp2.json()["added"] == 0
    assert len(store.load_instructor_candidates("95112")) == 1


def test_aha_leads_enter_instructor_match(client):
    client.get("/api/ops/zip/95112/aha-instructors?radius=25")
    match = client.get("/api/ops/zip/95112/instructor-match").json()
    names = [p.get("name") for p in match["best_instructor_path"]]
    assert "Beat CPR Training" in names


def test_draft_all_queues_but_never_sends(client):
    client.get("/api/ops/zip/95112/aha-instructors?radius=25")
    resp = client.post("/api/ops/zip/95112/aha-instructors/draft-all", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["queued_count"] == 1  # Beat CPR has an email
    # The draft is only PENDING_APPROVAL — nothing is sent by drafting.
    queue = client.get("/api/ops/outreach/queue").json()
    pending = queue["pending_approval"]
    assert any(v["to_email"] == "patty@beatcpr.com" for v in pending)
    assert all(v["status"] == "PENDING_APPROVAL" for v in pending)


def test_disabled_source_reports_gracefully(client, monkeypatch):
    monkeypatch.setattr(aha_atlas, "AHA_ATLAS_ENABLED", False)
    aha_atlas._CACHE.clear()
    out = aha_atlas.aha_instructor_candidates("95112")
    assert out["ok"] is False
    assert out["candidates"] == []
    assert "disabled" in out["note"].lower()
