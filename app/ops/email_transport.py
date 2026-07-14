"""
Email transport for the outreach engine (SMTP send + IMAP reply polling).

Everything network-y lives here so the engine itself stays pure and testable.
Configuration is env-only (never files, never the repo):

    OUTREACH_SEND_ENABLED   "1"/"true" to actually send; anything else = dry run
    OUTREACH_SMTP_USER      Gmail/Workspace address that sends the mail
    OUTREACH_SMTP_PASSWORD  app password (NOT the account password)
    OUTREACH_SMTP_HOST      default smtp.gmail.com
    OUTREACH_SMTP_PORT      default 587 (STARTTLS)
    OUTREACH_IMAP_HOST      default imap.gmail.com (reply polling; same creds)
    OUTREACH_FROM           From header, default OUTREACH_SMTP_USER
    OUTREACH_REPLY_TO       optional Reply-To

Dry-run mode (the default!) means the engine goes through every motion —
queue, approval, follow-up scheduling — but ``send`` only logs. Nothing can
email a real person until someone deliberately sets OUTREACH_SEND_ENABLED and
the SMTP credentials in the host environment.
"""
from __future__ import annotations

import email
import email.header
import imaplib
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def send_enabled() -> bool:
    return _env("OUTREACH_SEND_ENABLED").lower() in ("1", "true", "yes")


def smtp_configured() -> bool:
    return bool(_env("OUTREACH_SMTP_USER") and _env("OUTREACH_SMTP_PASSWORD"))


def transport_status() -> Dict[str, Any]:
    """Config health for the dashboard — booleans only, never secrets."""
    return {
        "send_enabled": send_enabled(),
        "smtp_configured": smtp_configured(),
        "imap_configured": smtp_configured(),  # same creds
        "from_address_set": bool(_env("OUTREACH_FROM")
                                 or _env("OUTREACH_SMTP_USER")),
        "mode": ("LIVE" if (send_enabled() and smtp_configured())
                 else "DRY_RUN"),
    }


class EmailTransport:
    """Real SMTP/IMAP transport. The engine holds one of these (or a test
    fake with the same two methods)."""

    def send(self, to_addr: str, subject: str, body: str) -> Dict[str, Any]:
        """Send one plain-text email. Returns {sent, mode, detail}."""
        if not send_enabled() or not smtp_configured():
            logger.info(f"outreach DRY RUN → {to_addr}: {subject}")
            return {"sent": False, "mode": "DRY_RUN",
                    "detail": "OUTREACH_SEND_ENABLED/SMTP creds not set"}
        user = _env("OUTREACH_SMTP_USER")
        msg = EmailMessage()
        msg["From"] = _env("OUTREACH_FROM") or user
        msg["To"] = to_addr
        msg["Subject"] = subject
        reply_to = _env("OUTREACH_REPLY_TO")
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        host = _env("OUTREACH_SMTP_HOST") or "smtp.gmail.com"
        port = int(_env("OUTREACH_SMTP_PORT") or "587")
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, _env("OUTREACH_SMTP_PASSWORD"))
            smtp.send_message(msg)
        logger.info(f"outreach SENT → {to_addr}: {subject}")
        return {"sent": True, "mode": "LIVE", "detail": ""}

    def fetch_replies(self, since: datetime) -> List[Dict[str, Any]]:
        """Fetch inbox messages since ``since`` (UTC). Returns
        [{from_email, subject, snippet, at}] — enough for the engine to match
        senders against lead emails; bodies stay out of the store."""
        if not smtp_configured():
            return []
        host = _env("OUTREACH_IMAP_HOST") or "imap.gmail.com"
        user = _env("OUTREACH_SMTP_USER")
        out: List[Dict[str, Any]] = []
        try:
            with imaplib.IMAP4_SSL(host) as imap:
                imap.login(user, _env("OUTREACH_SMTP_PASSWORD"))
                imap.select("INBOX", readonly=True)
                date_str = since.strftime("%d-%b-%Y")
                status, data = imap.search(None, f'(SINCE "{date_str}")')
                if status != "OK":
                    return []
                for num in (data[0].split() if data and data[0] else [])[-200:]:
                    status, msg_data = imap.fetch(
                        num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    parsed = email.message_from_bytes(msg_data[0][1])
                    _, from_email = parseaddr(str(parsed.get("From", "")))
                    subject_raw = parsed.get("Subject", "")
                    subject = str(email.header.make_header(
                        email.header.decode_header(subject_raw)))
                    out.append({
                        "from_email": from_email.lower(),
                        "subject": subject,
                        "snippet": "",
                        "at": datetime.now(timezone.utc).isoformat(),
                    })
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.warning(f"outreach reply poll failed: {exc}")
        return out
