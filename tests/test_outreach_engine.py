"""Outreach engine tests: queue guards, approval sending, follow-ups,
reply intake, opt-out, dry-run inertness, placeholder blocking, daily cap."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from app.ops import outreach_engine as eng
from app.ops import store


class FakeTransport:
    """Records sends; returns canned replies. sent=True mimics LIVE mode."""

    def __init__(self, sent: bool = True,
                 replies: List[Dict[str, Any]] | None = None):
        self.sent_flag = sent
        self.replies = replies or []
        self.outbox: List[Dict[str, str]] = []

    def send(self, to_addr, subject, body):
        self.outbox.append({"to": to_addr, "subject": subject, "body": body})
        return {"sent": self.sent_flag,
                "mode": "LIVE" if self.sent_flag else "DRY_RUN", "detail": ""}

    def fetch_replies(self, since):
        return self.replies


@pytest.fixture
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    monkeypatch.setenv("OUTREACH_STAFF_NAME", "Kedi")
    store.save_zip_candidates("INSTRUCTOR", "95112", [
        {"id": "i-1", "name": "Jane Doe", "source": "roster",
         "email": "jane@example.com", "outreach_status": "NEW"},
        {"id": "i-2", "name": "No Email Lead", "source": "roster",
         "outreach_status": "NEW"},
        {"id": "i-3", "name": "Already Replied", "source": "roster",
         "email": "replied@example.com", "outreach_status": "REPLIED"},
    ])
    store.save_zip_candidates("SPACE", "95112", [
        {"id": "s-1", "name": "Community Room A", "source": "manual",
         "email": "rooms@example.com", "outreach_status": "NEW"},
    ])
    return tmp_path


# --------------------------------------------------------------------------
# Queueing guards
# --------------------------------------------------------------------------
def test_enqueue_prepares_pending_sequence(ops_env):
    result = eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    assert result["ok"] is True
    assert result["placeholder_blocked"] is None  # staff name set via env
    snap = eng.queue_snapshot()
    assert len(snap["pending_approval"]) == 1
    assert snap["pending_approval"][0]["to_email"] == "jane@example.com"


def test_enqueue_refuses_no_email_and_replied(ops_env):
    assert eng.enqueue_lead("INSTRUCTOR", "i-2")["error"] == "no_email"
    assert eng.enqueue_lead("INSTRUCTOR", "i-3")["error"] == "status_blocks_queue"
    assert eng.enqueue_lead("INSTRUCTOR", "ghost")["error"] == "lead_not_found"


def test_enqueue_refuses_duplicates(ops_env):
    assert eng.enqueue_lead("INSTRUCTOR", "i-1")["ok"] is True
    assert eng.enqueue_lead("INSTRUCTOR", "i-1")["error"] == "already_queued"


def test_placeholder_blocks_send_until_staff_name(ops_env, monkeypatch):
    monkeypatch.delenv("OUTREACH_STAFF_NAME")
    result = eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    assert result["placeholder_blocked"] == "[Staff Name]"
    transport = FakeTransport(sent=True)
    approved = eng.approve_and_send(transport=transport)
    assert approved["sent"] == []
    assert approved["blocked"][0]["reason"] == "placeholder"
    assert transport.outbox == []  # nothing left the building


def test_edit_draft_unblocks(ops_env, monkeypatch):
    monkeypatch.delenv("OUTREACH_STAFF_NAME")
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    seq = eng._load_state()["sequences"]["i-1"]
    fixed = seq["body"].replace("[Staff Name]", "Kedi")
    result = eng.edit_queued_draft("i-1", body=fixed)
    assert result["placeholder_blocked"] is None


# --------------------------------------------------------------------------
# Approval + sending
# --------------------------------------------------------------------------
def test_approve_sends_and_advances_crm(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    result = eng.approve_and_send(transport=transport)
    assert [s["lead_id"] for s in result["sent"]] == ["i-1"]
    assert transport.outbox[0]["to"] == "jane@example.com"
    assert "Jane Doe" in transport.outbox[0]["body"]
    assert "[" not in transport.outbox[0]["body"]
    lead = store.find_lead("INSTRUCTOR", "i-1")
    assert lead["outreach_status"] == "CONTACTED"
    seq = eng._load_state()["sequences"]["i-1"]
    assert seq["status"] == "ACTIVE"
    assert seq["touch_count"] == 1
    assert seq["next_touch_at"] is not None
    log = store.load_outreach_log(target_id="i-1")
    assert log and log[-1]["status"] == "SENT"


def test_dry_run_approve_mutates_nothing(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=False)  # DRY_RUN transport behavior
    result = eng.approve_and_send(transport=transport)
    assert result["sent"] == []
    assert len(result["dry_run"]) == 1
    assert store.find_lead("INSTRUCTOR", "i-1")["outreach_status"] == "NEW"
    seq = eng._load_state()["sequences"]["i-1"]
    assert seq["status"] == "PENDING_APPROVAL"
    assert store.load_outreach_log(target_id="i-1") == []


def test_daily_cap(ops_env, monkeypatch):
    monkeypatch.setenv("OUTREACH_MAX_PER_DAY", "1")
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    eng.enqueue_lead("SPACE", "s-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    result = eng.approve_and_send(transport=transport)
    assert len(result["sent"]) == 1
    assert result["blocked"][0]["reason"] == "daily_cap"
    assert len(transport.outbox) == 1


def test_approve_subset_only(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    eng.enqueue_lead("SPACE", "s-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    result = eng.approve_and_send(lead_ids=["s-1"], transport=transport)
    assert [s["lead_id"] for s in result["sent"]] == ["s-1"]
    assert eng._load_state()["sequences"]["i-1"]["status"] == "PENDING_APPROVAL"


# --------------------------------------------------------------------------
# Follow-ups
# --------------------------------------------------------------------------
def _force_due(lead_id):
    state = eng._load_state()
    state["sequences"][lead_id]["next_touch_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    eng._save_state(state)


def test_tick_sends_due_follow_up_then_finishes(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    eng.approve_and_send(transport=transport)

    _force_due("i-1")
    result = eng.run_tick(transport=transport)
    assert result["followed_up"] == [{"lead_id": "i-1", "touch": 2}]
    assert transport.outbox[-1]["subject"].startswith("Re: ")
    assert "[" not in transport.outbox[-1]["body"]

    _force_due("i-1")
    result = eng.run_tick(transport=transport)
    assert result["followed_up"] == [{"lead_id": "i-1", "touch": 3}]
    seq = eng._load_state()["sequences"]["i-1"]
    assert seq["status"] == "DONE"        # 3 touches total, sequence over
    assert seq["next_touch_at"] is None

    result = eng.run_tick(transport=transport)
    assert result["followed_up"] == []    # never a 4th touch


def test_tick_not_due_sends_nothing(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    eng.approve_and_send(transport=transport)
    result = eng.run_tick(transport=transport)
    assert result["followed_up"] == []
    assert len(transport.outbox) == 1     # just the original touch


# --------------------------------------------------------------------------
# Reply intake
# --------------------------------------------------------------------------
def test_reply_stops_sequence_and_marks_crm(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    eng.approve_and_send(transport=transport)

    transport.replies = [{"from_email": "jane@example.com",
                          "subject": "Re: BLS opportunity — yes, interested!",
                          "snippet": "", "at": ""}]
    _force_due("i-1")
    result = eng.run_tick(transport=transport)
    assert result["replied"] == ["i-1"]
    assert result["followed_up"] == []    # reply beats the due follow-up
    lead = store.find_lead("INSTRUCTOR", "i-1")
    assert lead["outreach_status"] == "REPLIED"
    assert "[engine] reply received" in lead["notes"]
    assert eng._load_state()["sequences"]["i-1"]["status"] == "STOPPED_REPLIED"


def test_opt_out_reply_marks_not_interested(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    eng.approve_and_send(transport=transport)
    transport.replies = [{"from_email": "jane@example.com",
                          "subject": "please remove me from your list",
                          "snippet": "", "at": ""}]
    result = eng.run_tick(transport=transport)
    assert result["opted_out"] == ["i-1"]
    assert store.find_lead("INSTRUCTOR", "i-1")[
        "outreach_status"] == "NOT_INTERESTED"


def test_unknown_sender_ignored(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    transport = FakeTransport(sent=True)
    eng.approve_and_send(transport=transport)
    transport.replies = [{"from_email": "stranger@example.com",
                          "subject": "unrelated", "snippet": "", "at": ""}]
    result = eng.run_tick(transport=transport)
    assert result["replied"] == []
    assert store.find_lead("INSTRUCTOR", "i-1")[
        "outreach_status"] == "CONTACTED"


# --------------------------------------------------------------------------
# Cancel + status
# --------------------------------------------------------------------------
def test_cancel_pending_and_active(ops_env):
    eng.enqueue_lead("INSTRUCTOR", "i-1", zip_code="95112")
    assert eng.cancel_sequence("i-1")["ok"] is True
    assert eng._load_state()["sequences"]["i-1"]["status"] == "CANCELLED"
    assert eng.cancel_sequence("i-1")["error"] == "not_active"
    # A cancelled sequence can be re-queued deliberately.
    assert eng.enqueue_lead("INSTRUCTOR", "i-1")["ok"] is True


def test_engine_status_shape(ops_env):
    status = eng.engine_status()
    for key in ("mode", "send_enabled", "smtp_configured", "staff_name_set",
                "pending_approval", "active_sequences", "sends_today",
                "daily_cap", "follow_up_days"):
        assert key in status
    assert status["mode"] == "DRY_RUN"    # no SMTP creds in tests, ever


# --------------------------------------------------------------------------
# API layer (thin wrappers — just verify wiring + serialization)
# --------------------------------------------------------------------------
def test_api_wiring(ops_env, monkeypatch):
    from fastapi.testclient import TestClient
    import web_app
    client = TestClient(web_app.app)

    r = client.get("/api/ops/outreach/engine-status")
    assert r.status_code == 200 and r.json()["mode"] == "DRY_RUN"

    r = client.post("/api/ops/outreach/queue",
                    json={"lead_type": "instructor", "lead_id": "i-1",
                          "zip": "95112"})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get("/api/ops/outreach/queue")
    assert r.status_code == 200
    assert len(r.json()["pending_approval"]) == 1

    # DRY_RUN approve via the real transport: reports, mutates nothing.
    r = client.post("/api/ops/outreach/approve", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DRY_RUN" and len(body["dry_run"]) == 1

    r = client.post("/api/ops/outreach/tick")
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.post("/api/ops/outreach/queue/i-1/cancel")
    assert r.status_code == 200

    r = client.post("/api/ops/outreach/queue",
                    json={"lead_type": "bogus", "lead_id": "i-1"})
    assert r.status_code == 400
