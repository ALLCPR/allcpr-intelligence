"""
Optional write-token protection for dangerous ops endpoints.

The dashboard is served fully open (viewing + normal CRM edits need no login).
But some actions are dangerous on a public URL: uploading/overwriting the whole
store, sending real email, and writing to the Manatal ATS. Those should be
lockable without re-adding a login to everything.

Contract:
    OPS_WRITE_TOKEN unset  → open, exactly as today (nothing changes).
    OPS_WRITE_TOKEN set    → the guarded endpoints require a matching
                             ``X-Ops-Write-Token`` header; everything else
                             (all GETs, normal CRM writes) stays open.

The token is compared in constant time and never appears in any response,
log line, or error message. Scripts pass it via the same header from an env
var; the dashboard prompts for it once per session on the first guarded action.
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException

WRITE_TOKEN_HEADER = "X-Ops-Write-Token"


def write_token_configured() -> bool:
    return bool(os.environ.get("OPS_WRITE_TOKEN", "").strip())


def write_protection_status() -> dict:
    """Booleans only — safe to expose to the dashboard (never the token)."""
    return {
        "write_token_required": write_token_configured(),
        "header": WRITE_TOKEN_HEADER,
    }


def check_write_token(provided: Optional[str]) -> bool:
    """True when writes are allowed: token unset (open) or a correct match."""
    expected = os.environ.get("OPS_WRITE_TOKEN", "").strip()
    if not expected:
        return True
    return bool(provided) and secrets.compare_digest(str(provided).strip(),
                                                     expected)


def require_write_token(
    x_ops_write_token: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency guarding a dangerous endpoint.

    No-op when OPS_WRITE_TOKEN is unset (open). When set, a missing/wrong token
    is rejected with 401 — the detail never echoes the expected or provided
    value.
    """
    if not check_write_token(x_ops_write_token):
        raise HTTPException(
            status_code=401,
            detail=(f"This action is protected. Provide the {WRITE_TOKEN_HEADER} "
                    "header (set OPS_WRITE_TOKEN on the server; staff are "
                    "prompted once in the dashboard)."),
        )
