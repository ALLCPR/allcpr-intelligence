"""Optional write-token protection for dangerous ops endpoints (OPS_WRITE_TOKEN).

Contract: unset → open as today; set → guarded POSTs need the correct
X-Ops-Write-Token header, GETs stay open, and the token never leaks.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_app
from app.ops import store

_TICK = "/api/ops/outreach/tick"          # a guarded (dangerous) POST


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    return TestClient(web_app.app)


def test_open_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("OPS_WRITE_TOKEN", raising=False)
    assert client.post(_TICK).status_code == 200


def test_views_open_even_when_token_set(client, monkeypatch):
    monkeypatch.setenv("OPS_WRITE_TOKEN", "sekret")
    assert client.get("/").status_code == 200
    assert client.get("/api/ops/write-protection").status_code == 200


def test_dangerous_post_requires_token_when_set(client, monkeypatch):
    monkeypatch.setenv("OPS_WRITE_TOKEN", "sekret")
    assert client.post(_TICK).status_code == 401
    assert client.post(
        _TICK, headers={"X-Ops-Write-Token": "wrong"}).status_code == 401
    assert client.post(
        _TICK, headers={"X-Ops-Write-Token": "sekret"}).status_code == 200


def test_token_never_appears_in_responses(client, monkeypatch):
    monkeypatch.setenv("OPS_WRITE_TOKEN", "sup3r-secret-value")
    rejected = client.post(_TICK)
    assert rejected.status_code == 401
    assert "sup3r-secret-value" not in rejected.text
    status = client.get("/api/ops/write-protection")
    assert "sup3r-secret-value" not in status.text
    assert status.json() == {"write_token_required": True,
                             "header": "X-Ops-Write-Token"}
