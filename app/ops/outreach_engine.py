"""
Outreach engine: approval-gated sending + follow-up sequences + reply intake.

This upgrades outreach from "draft only" to a small recruiting machine while
keeping a human at the wheel:

    enqueue   staff (or future auto-sourcing) queues a lead → touch-1 email is
              prepared and waits in PENDING_APPROVAL. Nothing sends itself.
    approve   staff approves the batch → touch 1 actually sends; follow-up
              touches are scheduled. Approving the sequence once authorizes
              its remaining touches.
    tick      cron/manual: polls the inbox for replies (reply → CRM REPLIED,
              sequence stops; opt-out wording → NOT_INTERESTED) and sends any
              follow-up touches that have come due.

Safety rails, in order of importance:
    - Dry run by default: until OUTREACH_SEND_ENABLED + SMTP creds are set in
      the environment, approving/ticking reports what WOULD send and mutates
      nothing.
    - An email containing an unresolved [placeholder] is never sent — leads
      whose draft still has brackets stay blocked until the draft is edited
      (set OUTREACH_STAFF_NAME so drafts don't carry "[Staff Name]").
    - Daily send cap (OUTREACH_MAX_PER_DAY, default 25) so a big queue can't
      turn the Gmail account into a spam cannon.
    - One sequence per lead; leads that replied / declined / were rejected
      can't be re-queued; a reply stops the sequence immediately.

Sequence state lives in the ops store (data/ops/outreach_sequences.json) next
to the CRM it drives.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.ops import store
from app.ops.email_transport import EmailTransport, transport_status
from app.ops.models import OutreachLog, utc_now_iso
from app.ops.outreach_templates import (
    generate_instructor_outreach,
    generate_space_outreach,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

SEQUENCES_FILE = "outreach_sequences.json"

# Touch schedule: touch 1 on approval, then follow-ups this many days apart.
DEFAULT_FOLLOW_UP_DAYS = (3, 5)          # → 3 touches total
DEFAULT_MAX_PER_DAY = 25

# A lead in one of these CRM states must not be (re-)emailed automatically.
DO_NOT_QUEUE_STATUSES = frozenset({
    "REPLIED", "INTERESTED", "NOT_INTERESTED", "CONFIRMED", "REJECTED",
    "CREDENTIAL_VERIFIED", "AVAILABLE", "GOOD_FIT",
})

# Reply wording that means "stop contacting me" rather than "let's talk".
_OPT_OUT_RE = re.compile(
    r"unsubscribe|remove me|stop email|do not (?:contact|email)|"
    r"not interested|no longer interested", re.I)

_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,60}\]")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _follow_up_days() -> tuple:
    raw = os.environ.get("OUTREACH_FOLLOW_UP_DAYS", "").strip()
    if not raw:
        return DEFAULT_FOLLOW_UP_DAYS
    try:
        days = tuple(int(p) for p in raw.split(",") if p.strip())
        return days or DEFAULT_FOLLOW_UP_DAYS
    except ValueError:
        return DEFAULT_FOLLOW_UP_DAYS


def _max_per_day() -> int:
    try:
        return max(1, int(os.environ.get("OUTREACH_MAX_PER_DAY",
                                         str(DEFAULT_MAX_PER_DAY))))
    except ValueError:
        return DEFAULT_MAX_PER_DAY


def _staff_name() -> str:
    return os.environ.get("OUTREACH_STAFF_NAME", "").strip() or "[Staff Name]"


# --------------------------------------------------------------------------
# Sequence state (one JSON file in the ops store)
# --------------------------------------------------------------------------
def _load_state(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    state = store._read_json(store._ops_dir(base_dir) / SEQUENCES_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("sequences", {})
    state.setdefault("daily_sends", {})
    return state


def _save_state(state: Dict[str, Any], base_dir: Optional[Path] = None) -> None:
    store._write_json_atomic(store._ops_dir(base_dir) / SEQUENCES_FILE, state)


def _today_key(now: datetime) -> str:
    return now.date().isoformat()


def _sends_today(state: Dict[str, Any], now: datetime) -> int:
    return int(state["daily_sends"].get(_today_key(now)) or 0)


def _count_send(state: Dict[str, Any], now: datetime) -> None:
    key = _today_key(now)
    state["daily_sends"] = {key: state["daily_sends"].get(key, 0) + 1}


def _has_placeholders(subject: str, body: str) -> Optional[str]:
    hit = _PLACEHOLDER_RE.search(subject) or _PLACEHOLDER_RE.search(body)
    return hit.group(0) if hit else None


def _sendable_greeting_fix(body: str, lead: Dict[str, Any]) -> str:
    """Business/signal leads draft as "Hi [Program Coordinator]," etc. For a
    named business (live-sourced CPR provider, coworking space) a team
    greeting is honest and sendable; the placeholder is not."""
    name = str(lead.get("name") or "").strip()
    if not name or "[" in name:
        return body
    return re.sub(r"^Hi \[[^\]\n]{1,60}\],", f"Hi {name} team,", body, count=1)


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------
def enqueue_lead(target_type: str, lead_id: str, zip_code: str = "",
                 created_by: str = "", base_dir: Optional[Path] = None
                 ) -> Dict[str, Any]:
    """Prepare touch 1 for a lead and place it in the approval queue."""
    lead = store.find_lead(target_type, lead_id, base_dir)
    if not lead:
        return {"ok": False, "error": "lead_not_found"}
    to_email = str(lead.get("email") or "").strip().lower()
    if not to_email or "@" not in to_email:
        return {"ok": False, "error": "no_email",
                "message": "Lead has no email address — add one via the CRM "
                           "before queueing outreach."}
    if str(lead.get("outreach_status") or "NEW") in DO_NOT_QUEUE_STATUSES:
        return {"ok": False, "error": "status_blocks_queue",
                "message": f"Lead status {lead.get('outreach_status')} — "
                           "engine will not re-email this lead."}
    state = _load_state(base_dir)
    seq = state["sequences"].get(lead_id)
    if seq and seq.get("status") in ("PENDING_APPROVAL", "ACTIVE"):
        return {"ok": False, "error": "already_queued"}

    if target_type == "INSTRUCTOR":
        draft = generate_instructor_outreach(lead, zip_code=zip_code,
                                             staff_name=_staff_name())
    else:
        draft = generate_space_outreach(lead, zip_code=zip_code,
                                        staff_name=_staff_name())
    body = _sendable_greeting_fix(draft["body"], lead)
    state["sequences"][lead_id] = {
        "lead_id": lead_id,
        "target_type": target_type,
        "zip": zip_code or str(lead.get("zip") or ""),
        "lead_name": str(lead.get("name") or ""),
        "to_email": to_email,
        "subject": draft["subject"],
        "body": body,
        "status": "PENDING_APPROVAL",
        "touch_count": 0,
        "next_touch_at": None,
        "created_at": utc_now_iso(),
        "created_by": created_by,
        "history": [],
    }
    _save_state(state, base_dir)
    blocked = _has_placeholders(draft["subject"], body)
    return {"ok": True, "lead_id": lead_id, "to_email": to_email,
            "subject": draft["subject"],
            "placeholder_blocked": blocked,
            "message": (f"Queued, but blocked from sending until "
                        f"'{blocked}' is resolved (edit the draft or set "
                        f"OUTREACH_STAFF_NAME)." if blocked
                        else "Queued for approval.")}


def edit_queued_draft(lead_id: str, subject: str = "", body: str = "",
                      base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Replace the prepared touch-1 draft (to resolve placeholders)."""
    state = _load_state(base_dir)
    seq = state["sequences"].get(lead_id)
    if not seq or seq.get("status") != "PENDING_APPROVAL":
        return {"ok": False, "error": "not_pending"}
    if subject.strip():
        seq["subject"] = subject.strip()
    if body.strip():
        seq["body"] = body.strip()
    _save_state(state, base_dir)
    return {"ok": True, "lead_id": lead_id,
            "placeholder_blocked": _has_placeholders(seq["subject"],
                                                     seq["body"])}


def cancel_sequence(lead_id: str, base_dir: Optional[Path] = None
                    ) -> Dict[str, Any]:
    state = _load_state(base_dir)
    seq = state["sequences"].get(lead_id)
    if not seq or seq.get("status") not in ("PENDING_APPROVAL", "ACTIVE"):
        return {"ok": False, "error": "not_active"}
    seq["status"] = "CANCELLED"
    seq["next_touch_at"] = None
    _save_state(state, base_dir)
    return {"ok": True, "lead_id": lead_id}


def queue_snapshot(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Everything the dashboard needs to render the queue."""
    state = _load_state(base_dir)
    now = _now()
    pending, active, finished = [], [], []
    for seq in state["sequences"].values():
        view = {k: seq.get(k) for k in (
            "lead_id", "target_type", "zip", "lead_name", "to_email",
            "subject", "status", "touch_count", "next_touch_at",
            "created_at")}
        view["placeholder_blocked"] = _has_placeholders(
            seq.get("subject", ""), seq.get("body", ""))
        view["body_preview"] = (seq.get("body") or "")[:400]
        if seq.get("status") == "PENDING_APPROVAL":
            pending.append(view)
        elif seq.get("status") == "ACTIVE":
            active.append(view)
        else:
            finished.append(view)
    finished.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    return {
        "engine": engine_status(base_dir),
        "pending_approval": sorted(pending,
                                   key=lambda v: v.get("created_at") or ""),
        "active_sequences": sorted(active,
                                   key=lambda v: v.get("next_touch_at") or ""),
        "recently_finished": finished[:20],
        "sends_today": _sends_today(state, now),
    }


def engine_status(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    state = _load_state(base_dir)
    now = _now()
    seqs = state["sequences"].values()
    return {
        **transport_status(),
        "staff_name_set": _staff_name() != "[Staff Name]",
        "pending_approval": sum(1 for s in seqs
                                if s.get("status") == "PENDING_APPROVAL"),
        "active_sequences": sum(1 for s in seqs
                                if s.get("status") == "ACTIVE"),
        "sends_today": _sends_today(state, now),
        "daily_cap": _max_per_day(),
        "follow_up_days": list(_follow_up_days()),
    }


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
def _record_sent_touch(seq: Dict[str, Any], touch_n: int, subject: str,
                       now: datetime, base_dir: Optional[Path]) -> None:
    total_touches = 1 + len(_follow_up_days())
    seq["touch_count"] = touch_n
    seq["history"].append({"touch": touch_n, "at": now.isoformat(),
                           "subject": subject})
    if touch_n >= total_touches:
        seq["status"] = "DONE"
        seq["next_touch_at"] = None
    else:
        seq["status"] = "ACTIVE"
        gap_days = _follow_up_days()[touch_n - 1]
        seq["next_touch_at"] = (now + timedelta(days=gap_days)).isoformat()
    entry = OutreachLog(
        target_type=seq["target_type"], target_id=seq["lead_id"],
        channel="EMAIL",
        message_template=f"engine_touch_{touch_n}",
        message_text=f"Subject: {subject}", status="SENT",
        sent_at=now.isoformat(),
        next_followup_at=seq.get("next_touch_at"),
        created_by="outreach_engine",
    ).to_dict()
    store.append_outreach_log(entry, base_dir)


def approve_and_send(lead_ids: Optional[List[str]] = None, sent_by: str = "",
                     transport: Optional[EmailTransport] = None,
                     base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Send touch 1 for pending sequences (all, or just ``lead_ids``)."""
    transport = transport or EmailTransport()
    state = _load_state(base_dir)
    now = _now()
    cap = _max_per_day()
    sent, blocked, dry_run = [], [], []
    for lead_id, seq in state["sequences"].items():
        if seq.get("status") != "PENDING_APPROVAL":
            continue
        if lead_ids is not None and lead_id not in lead_ids:
            continue
        placeholder = _has_placeholders(seq.get("subject", ""),
                                        seq.get("body", ""))
        if placeholder:
            blocked.append({"lead_id": lead_id, "reason": "placeholder",
                            "detail": placeholder})
            continue
        if _sends_today(state, now) >= cap:
            blocked.append({"lead_id": lead_id, "reason": "daily_cap",
                            "detail": f"{cap}/day reached"})
            continue
        result = transport.send(seq["to_email"], seq["subject"], seq["body"])
        if not result.get("sent"):
            dry_run.append({"lead_id": lead_id, "to": seq["to_email"],
                            "subject": seq["subject"],
                            "detail": result.get("detail", "")})
            continue
        _count_send(state, now)
        _record_sent_touch(seq, 1, seq["subject"], now, base_dir)
        store.update_lead(seq["target_type"], lead_id,
                          {"outreach_status": "CONTACTED"}, base_dir)
        sent.append({"lead_id": lead_id, "to": seq["to_email"]})
    _save_state(state, base_dir)
    logger.info(f"outreach approve: sent={len(sent)} dry_run={len(dry_run)} "
                f"blocked={len(blocked)}")
    return {"ok": True, "sent": sent, "dry_run": dry_run, "blocked": blocked,
            "mode": transport_status()["mode"]}


def _follow_up_body(seq: Dict[str, Any], touch_n: int) -> str:
    first = ("Just floating this back to the top of your inbox — we're "
             "still looking for CPR/BLS instructors"
             if seq["target_type"] == "INSTRUCTOR"
             else "Just floating this back to the top of your inbox — we're "
                  "still looking for a recurring classroom")
    if touch_n >= 1 + len(_follow_up_days()):
        first = ("Last note from me — if the timing isn't right, no reply "
                 "needed and I won't follow up again. We're still interested"
                 if seq["target_type"] == "INSTRUCTOR"
                 else "Last note from me — if the timing isn't right, no "
                      "reply needed and I won't follow up again. We're still "
                      "looking for a recurring classroom")
    area = f" near {seq['zip']}" if seq.get("zip") else ""
    staff = _staff_name()
    return (f"Hi,\n\n{first}{area} and your details are exactly what we "
            f"need to move forward (see my earlier email below the subject "
            f"line).\n\nThank you,\n{staff}\nALLCPR")


def run_tick(transport: Optional[EmailTransport] = None,
             base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One engine heartbeat: intake replies, then send due follow-ups."""
    transport = transport or EmailTransport()
    state = _load_state(base_dir)
    now = _now()

    # ---- Reply intake -----------------------------------------------------
    replied, opted_out = [], []
    since_iso = state.get("last_reply_poll_at")
    try:
        since = datetime.fromisoformat(since_iso) if since_iso else (
            now - timedelta(days=7))
    except ValueError:
        since = now - timedelta(days=7)
    # Overlap one day so boundary messages are never missed; matching is
    # idempotent (a stopped sequence stays stopped).
    replies = transport.fetch_replies(since - timedelta(days=1))
    if replies:
        by_email: Dict[str, Dict[str, Any]] = {}
        for seq in state["sequences"].values():
            if seq.get("status") in ("PENDING_APPROVAL", "ACTIVE", "DONE"):
                by_email[seq["to_email"]] = seq
        for reply in replies:
            seq = by_email.get(str(reply.get("from_email") or "").lower())
            if not seq or seq.get("status") in ("STOPPED_REPLIED",
                                                "STOPPED_OPT_OUT"):
                continue
            text = f"{reply.get('subject', '')} {reply.get('snippet', '')}"
            opt_out = bool(_OPT_OUT_RE.search(text))
            seq["status"] = "STOPPED_OPT_OUT" if opt_out else "STOPPED_REPLIED"
            seq["next_touch_at"] = None
            new_status = "NOT_INTERESTED" if opt_out else "REPLIED"
            lead = store.find_lead(seq["target_type"], seq["lead_id"],
                                   base_dir) or {}
            prior_notes = str(lead.get("notes") or "").strip()
            note = (f"[engine] reply received {now.date().isoformat()}: "
                    f"{reply.get('subject', '')[:120]}")
            store.update_lead(seq["target_type"], seq["lead_id"],
                              {"outreach_status": new_status,
                               "notes": f"{prior_notes}\n{note}".strip()},
                              base_dir)
            (opted_out if opt_out else replied).append(seq["lead_id"])
    state["last_reply_poll_at"] = now.isoformat()

    # ---- Due follow-ups ---------------------------------------------------
    followed_up, dry_run, capped = [], [], []
    cap = _max_per_day()
    for lead_id, seq in state["sequences"].items():
        if seq.get("status") != "ACTIVE" or not seq.get("next_touch_at"):
            continue
        try:
            due = datetime.fromisoformat(seq["next_touch_at"])
        except ValueError:
            continue
        if due > now:
            continue
        if _sends_today(state, now) >= cap:
            capped.append(lead_id)
            continue
        touch_n = int(seq.get("touch_count") or 0) + 1
        subject = f"Re: {seq['subject']}"
        result = transport.send(seq["to_email"], subject,
                                _follow_up_body(seq, touch_n))
        if not result.get("sent"):
            dry_run.append({"lead_id": lead_id, "touch": touch_n,
                            "to": seq["to_email"]})
            continue
        _count_send(state, now)
        _record_sent_touch(seq, touch_n, subject, now, base_dir)
        followed_up.append({"lead_id": lead_id, "touch": touch_n})
    _save_state(state, base_dir)
    logger.info(f"outreach tick: replies={len(replied)} "
                f"opt_out={len(opted_out)} follow_ups={len(followed_up)} "
                f"dry_run={len(dry_run)} capped={len(capped)}")
    return {"ok": True, "replied": replied, "opted_out": opted_out,
            "followed_up": followed_up, "dry_run": dry_run,
            "capped": capped, "mode": transport_status()["mode"]}
