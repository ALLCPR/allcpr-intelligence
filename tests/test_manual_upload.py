"""Uploading the real source CSVs to a hosted instance's disk (import-manual)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import imports, store


@pytest.fixture
def manual_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "MANUAL_DIR", tmp_path / "manual")
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    return tmp_path / "manual"


@pytest.fixture
def client():
    return TestClient(web_app.app)


def test_save_manual_csv_whitelist(manual_dir):
    ok, _ = imports.save_manual_csv("instructor_performance.csv",
                                    "name,students\nJane,10\n")
    assert ok is True
    assert (manual_dir / "instructor_performance.csv").exists()

    # non-whitelisted name rejected
    ok, reason = imports.save_manual_csv("evil.csv", "x")
    assert ok is False and reason == "not_whitelisted"

    # path traversal is stripped to a basename → still rejected
    ok, reason = imports.save_manual_csv("../../etc/passwd", "x")
    assert ok is False and reason == "not_whitelisted"


def test_import_manual_endpoint_writes(manual_dir, client):
    resp = client.post("/api/ops/admin/import-manual",
                       json={"files": {"allcpr_locations.csv":
                                       "location_name,zip\nA,94541\n"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and "allcpr_locations.csv" in body["written"]
    assert (manual_dir / "allcpr_locations.csv").exists()


def test_import_manual_rejects_nonwhitelisted(manual_dir, client):
    resp = client.post("/api/ops/admin/import-manual",
                       json={"files": {"secrets.csv": "x"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["skipped"]["secrets.csv"] == "not_whitelisted"


def test_import_manual_is_write_guarded(manual_dir, client, monkeypatch):
    monkeypatch.setenv("OPS_WRITE_TOKEN", "sekret")
    payload = {"files": {"allcpr_locations.csv": "location_name,zip\nA,94541\n"}}
    assert client.post("/api/ops/admin/import-manual",
                       json=payload).status_code == 401
    assert client.post("/api/ops/admin/import-manual", json=payload,
                       headers={"X-Ops-Write-Token": "sekret"}).status_code == 200
