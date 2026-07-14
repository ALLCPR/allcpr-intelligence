"""
Manatal ATS HTTP client — SAFE / OFF BY DEFAULT.

The ALLCPR engine finds instructor/professor leads; Manatal is the recruiting
tracker they get pushed into. This module is the only place that talks to the
Manatal API, and it is deliberately inert unless explicitly switched on.

⚠️ SECURITY (read before use):
  * The Manatal API key/token is a SECRET. It comes ONLY from the environment
    (MANATAL_API_KEY or MANATAL_ACCESS_TOKEN) — never from code, git,
    render.yaml, the CSV imports, logs, responses, or the UI.
  * A Manatal credential was exposed in a Slack screenshot. Treat it as
    compromised and ROTATE it before any production use, then store the new
    value only in Render env vars / a local .env.
  * The token is never returned by any function here, never logged, and is
    scrubbed from error text as a last resort.

Gating (both off by default):
    MANATAL_ENABLED=1        allow read calls at all (else every call is a safe
                             "disabled" no-op)
    MANATAL_WRITE_ENABLED=1  allow create/update calls (else writes are a
                             dry-run that returns the payload WOULD-send only)

Other env:
    MANATAL_API_BASE_URL     e.g. https://api.manatal.com
    MANATAL_API_KEY / MANATAL_ACCESS_TOKEN   the secret token
    MANATAL_AUTH_SCHEME      default "Token" (Manatal); "Bearer" also supported
    MANATAL_DEFAULT_OWNER_ID / MANATAL_DEFAULT_ORGANIZATION   optional
    MANATAL_CANDIDATES_PATH / MANATAL_JOBS_PATH   API paths (sane defaults)
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 20
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5

# Transport signature: (method, url, headers, json, timeout) -> (status, data)
Transport = Callable[[str, str, Dict[str, str], Optional[dict], int],
                     Tuple[int, Any]]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def enabled() -> bool:
    return _env("MANATAL_ENABLED").lower() in ("1", "true", "yes")


def write_enabled() -> bool:
    return _env("MANATAL_WRITE_ENABLED").lower() in ("1", "true", "yes")


def _token() -> str:
    return _env("MANATAL_API_KEY") or _env("MANATAL_ACCESS_TOKEN")


def _base_url() -> str:
    return _env("MANATAL_API_BASE_URL").rstrip("/")


def configured() -> bool:
    return bool(_base_url() and _token())


def status() -> Dict[str, Any]:
    """Config health — booleans + non-secret ids only. Never the token."""
    mode = "DISABLED"
    if enabled() and configured():
        mode = "LIVE_WRITE" if write_enabled() else "READ_ONLY"
    return {
        "enabled": enabled(),
        "write_enabled": write_enabled(),
        "configured": configured(),
        "base_url_set": bool(_base_url()),
        "default_owner_set": bool(_env("MANATAL_DEFAULT_OWNER_ID")),
        "default_organization": _env("MANATAL_DEFAULT_ORGANIZATION") or None,
        "mode": mode,
    }


def _scrub(text: Any) -> str:
    """Belt-and-suspenders: strip the token from any string before it leaves."""
    s = str(text)
    tok = _token()
    if tok and tok in s:
        s = s.replace(tok, "***")
    return s


def _disabled_response(action: str) -> Dict[str, Any]:
    return {"ok": False, "disabled": True, "mode": "DISABLED", "action": action,
            "message": ("Manatal integration disabled. Set MANATAL_ENABLED=1 "
                        "and credentials in the environment.")}


def _not_configured_response(action: str) -> Dict[str, Any]:
    return {"ok": False, "error": "not_configured", "action": action,
            "message": ("Manatal is enabled but MANATAL_API_BASE_URL and "
                        "MANATAL_API_KEY/MANATAL_ACCESS_TOKEN are not set.")}


def _dry_run_response(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "dry_run": True, "mode": "READ_ONLY", "action": action,
            "would_send": payload,
            "message": ("Write dry-run — set MANATAL_WRITE_ENABLED=1 to create "
                        "or update real Manatal records.")}


def _requests_transport(method: str, url: str, headers: Dict[str, str],
                        json: Optional[dict], timeout: int) -> Tuple[int, Any]:
    import requests  # noqa: PLC0415 — deferred so import stays cheap/optional
    resp = requests.request(method, url, headers=headers, json=json,
                            timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


class ManatalClient:
    """Thin, gated wrapper over the Manatal REST API.

    Every method returns a plain dict; write methods respect
    MANATAL_WRITE_ENABLED. Inject ``transport`` in tests to avoid real HTTP.
    """

    def __init__(self, transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._transport = transport or _requests_transport
        self._sleep = sleep

    # -- low level --------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        scheme = _env("MANATAL_AUTH_SCHEME") or "Token"
        return {"Authorization": f"{scheme} {_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def _request(self, method: str, path: str, *, write: bool,
                 json: Optional[dict] = None,
                 action: str = "") -> Dict[str, Any]:
        action = action or f"{method} {path}"
        if not enabled():
            return _disabled_response(action)
        if not configured():
            return _not_configured_response(action)
        if write and not write_enabled():
            return _dry_run_response(action, json or {})

        url = _base_url() + path
        headers = self._headers()
        last_err = ""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                code, data = self._transport(method, url, headers, json,
                                             _DEFAULT_TIMEOUT)
            except Exception as exc:  # noqa: BLE0001 — network/transport errors
                last_err = _scrub(f"{type(exc).__name__}: {exc}")
                logger.warning(f"manatal {action} attempt {attempt} failed: "
                               f"{last_err}")
                if attempt < _MAX_RETRIES:
                    self._sleep(_RETRY_BACKOFF * attempt)
                continue
            if code >= 500 and attempt < _MAX_RETRIES:
                last_err = f"HTTP {code}"
                self._sleep(_RETRY_BACKOFF * attempt)
                continue
            if 200 <= code < 300:
                return {"ok": True, "status_code": code, "data": data,
                        "action": action}
            logger.warning(f"manatal {action} HTTP {code}: "
                           f"{_scrub(data)[:300]}")
            return {"ok": False, "status_code": code, "action": action,
                    "message": _scrub(data)[:500]}
        return {"ok": False, "error": "request_failed", "action": action,
                "message": last_err or "Manatal request failed after retries."}

    # -- path helpers (Manatal Open API v3; overridable via env) ----------
    # Real base: https://api.manatal.com/open/v3/ — set MANATAL_API_BASE_URL to
    # https://api.manatal.com and these paths resolve correctly.
    def _candidates_base(self) -> str:
        return _env("MANATAL_CANDIDATES_PATH") or "/open/v3/candidates/"

    def _jobs_path(self) -> str:
        return _env("MANATAL_JOBS_PATH") or "/open/v3/jobs/"

    def _matches_path(self) -> str:
        return _env("MANATAL_MATCHES_PATH") or "/open/v3/matches/"

    def _organizations_path(self) -> str:
        return _env("MANATAL_ORGANIZATIONS_PATH") or "/open/v3/organizations/"

    def _users_path(self) -> str:
        return _env("MANATAL_USERS_PATH") or "/open/v3/users/"

    # -- organizations / users ---------------------------------------------
    def list_organizations(self, limit: int = 2) -> Dict[str, Any]:
        """List client organizations (jobs REQUIRE an organization id)."""
        return self._request("GET",
                             f"{self._organizations_path()}?limit={limit}",
                             write=False, action="list_organizations")

    def list_users(self, limit: int = 50) -> Dict[str, Any]:
        """List account users (to find an owner id for jobs/candidates)."""
        return self._request("GET", f"{self._users_path()}?limit={limit}",
                             write=False, action="list_users")

    # -- jobs -------------------------------------------------------------
    def list_jobs(self, limit: int = 5) -> Dict[str, Any]:
        """Sample existing jobs (read-only) — new jobs copy their org/owner."""
        return self._request("GET", f"{self._jobs_path()}?limit={limit}",
                             write=False, action="list_jobs")

    def create_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self._jobs_path(), write=True,
                             json=payload, action="create_job")

    def add_job_note(self, job_id: str, note: str) -> Dict[str, Any]:
        # POST /open/v3/jobs/{id}/notes/
        return self._request("POST", f"{self._jobs_path()}{job_id}/notes/",
                             write=True, json={"note": note},
                             action="add_job_note")

    # -- candidates -------------------------------------------------------
    def create_candidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self._candidates_base(), write=True,
                             json=payload, action="create_candidate")

    def update_candidate(self, candidate_id: str,
                         payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH",
                             f"{self._candidates_base()}{candidate_id}/",
                             write=True, json=payload,
                             action="update_candidate")

    def delete_candidate(self, candidate_id: str) -> Dict[str, Any]:
        return self._request("DELETE",
                             f"{self._candidates_base()}{candidate_id}/",
                             write=True, action="delete_candidate")

    def add_candidate_note(self, candidate_id: str,
                           note: str) -> Dict[str, Any]:
        # POST /open/v3/candidates/{id}/notes/
        return self._request(
            "POST", f"{self._candidates_base()}{candidate_id}/notes/",
            write=True, json={"note": note}, action="add_candidate_note")

    def get_candidate(self, candidate_id: str) -> Dict[str, Any]:
        return self._request("GET",
                             f"{self._candidates_base()}{candidate_id}/",
                             write=False, action="get_candidate")

    # -- matches (attach candidate to a job + read recruitment stage) -----
    def create_match(self, candidate_id: str, job_id: str) -> Dict[str, Any]:
        """Attach a candidate to a job — creates a pipeline match."""
        return self._request("POST", self._matches_path(), write=True,
                             json={"candidate": candidate_id, "job": job_id},
                             action="create_match")

    def get_candidate_matches(self, candidate_id: str) -> Dict[str, Any]:
        """A candidate's job matches; each carries its recruitment stage."""
        return self._request(
            "GET", f"{self._candidates_base()}{candidate_id}/matches/",
            write=False, action="get_candidate_matches")

    def test_connection(self) -> Dict[str, Any]:
        """Read-only auth/reachability probe: list one candidate.

        A 2xx means the token authenticates and the base URL is right; used by
        the /api/ops/manatal/test endpoint (which returns only ok/count, never
        candidate data).
        """
        return self._request("GET", f"{self._candidates_base()}?limit=1",
                             write=False, action="test_connection")
