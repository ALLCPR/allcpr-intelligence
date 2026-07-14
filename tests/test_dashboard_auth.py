"""Tests for the (intentionally open) hosted dashboard + ops-store import API.

The tool is served with no site-wide auth, by request — every route, including
the store-import endpoint, is reachable without a credential. These tests lock
in that contract (so a password gate isn't reintroduced by accident) and cover
the import endpoint's merge/replace + validation behavior.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import store

_SNAPSHOT = {
    "instructor_candidates": {
        "95112": [{"id": "i-1", "name": "Jane Doe", "source": "roster",
                   "credential_status": "VERIFIED",
                   "outreach_status": "CONFIRMED"}],
    },
    "space_candidates": {
        "95112": [{"id": "s-1", "name": "Community Room A",
                   "source": "manual", "outreach_status": "NEW"}],
    },
    "outreach_log": [{"id": "o-1", "target_id": "i-1", "channel": "email"}],
    "refresh_state": {"95112": "2026-07-01T00:00:00+00:00"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    return TestClient(web_app.app)


# --------------------------------------------------------------------------
# No auth anywhere — views and writes are both open.
# --------------------------------------------------------------------------
def test_views_are_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_writes_are_open_even_with_stray_password_env(client, monkeypatch):
    # A leftover DASHBOARD_PASSWORD in the environment must NOT re-gate anything
    # — the middleware is gone, so it has no effect.
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    assert client.post("/api/ops/outreach/tick").status_code == 200
    assert client.post(
        "/api/ops/admin/import-store", json=_SNAPSHOT).status_code == 200


# --------------------------------------------------------------------------
# Ops-store import endpoint (open — no credential required)
# --------------------------------------------------------------------------
def test_import_writes_snapshot(client):
    resp = client.post("/api/ops/admin/import-store", json=_SNAPSHOT)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["imported"]["instructor_candidates"] == 1
    assert body["imported"]["space_candidates"] == 1
    rows = store.load_instructor_candidates("95112")
    assert [r["name"] for r in rows] == ["Jane Doe"]
    assert store.load_outreach_log()[0]["id"] == "o-1"
    assert store.last_refresh_at("95112") == "2026-07-01T00:00:00+00:00"


def test_import_merge_preserves_live_crm_edits(client):
    # Staff on the live site already moved this lead forward.
    store.save_zip_candidates("INSTRUCTOR", "95112", [
        {"id": "i-1", "name": "Jane Doe", "source": "roster",
         "outreach_status": "REPLIED", "notes": "left voicemail"}])
    snapshot = {"instructor_candidates": {
        "95112": [{"id": "i-1", "name": "Jane Doe", "source": "roster",
                   "outreach_status": "NEW", "hourly_rate": 65}]}}
    resp = client.post("/api/ops/admin/import-store", json=snapshot)
    assert resp.status_code == 200
    row = store.load_instructor_candidates("95112")[0]
    assert row["outreach_status"] == "REPLIED"   # live edit kept
    assert row["notes"] == "left voicemail"
    assert row["hourly_rate"] == 65              # new local data arrived


def test_import_replace_overwrites(client):
    store.save_zip_candidates("INSTRUCTOR", "95110", [
        {"id": "i-9", "name": "Old Lead", "source": "roster",
         "outreach_status": "REPLIED"}])
    resp = client.post("/api/ops/admin/import-store?mode=replace",
                       json=_SNAPSHOT)
    assert resp.status_code == 200
    assert store.load_instructor_candidates("95110") == []
    assert len(store.load_instructor_candidates("95112")) == 1


def test_import_rejects_bad_mode_and_payload(client):
    assert client.post("/api/ops/admin/import-store?mode=destroy",
                       json=_SNAPSHOT).status_code == 400
    assert client.post("/api/ops/admin/import-store",
                       json={"nonsense": 1}).status_code == 400


def test_ops_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path / "disk-ops"))
    import importlib
    mod = importlib.reload(store)
    try:
        assert mod.OPS_DATA_DIR == tmp_path / "disk-ops"
    finally:
        monkeypatch.delenv("OPS_DATA_DIR")
        importlib.reload(store)
