"""
JSON-file persistence for the expansion-operations layer.

The main web app stays a thin layer over pre-generated JSON; the ops layer is
the one deliberately mutable part (CRM statuses, outreach log, lead refreshes),
so it gets its own small store under ``data/ops/`` — plain JSON files, atomic
writes, no database. Everything under ``data/ops/`` is runtime state and is
gitignored; tracked examples live in ``examples/ops/``.

Files:
    data/ops/instructor_candidates.json   {zip: [candidate dicts]}
    data/ops/space_candidates.json        {zip: [space dicts]}
    data/ops/outreach_log.json            [outreach log dicts]
    data/ops/refresh_state.json           {zip: iso timestamp of last refresh}
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR
from app.ops.models import utc_now_iso
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Overridable so hosted deployments can point the mutable store at a
# persistent disk (Render wipes the repo checkout on every deploy; only the
# mounted disk survives). Locally this stays data/ops/.
OPS_DATA_DIR = Path(os.environ.get("OPS_DATA_DIR") or (DATA_DIR / "ops"))

INSTRUCTOR_FILE = "instructor_candidates.json"
SPACE_FILE = "space_candidates.json"
OUTREACH_LOG_FILE = "outreach_log.json"
REFRESH_STATE_FILE = "refresh_state.json"

# Minimum seconds between automatic refreshes of one ZIP (POST /refresh
# without force). The refresh is offline/local today, but the gate keeps the
# endpoint safe if live sources are ever wired in.
REFRESH_MIN_INTERVAL_SECONDS = 15 * 60


def _ops_dir(base_dir: Optional[Path] = None) -> Path:
    return Path(base_dir) if base_dir is not None else OPS_DATA_DIR


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"ops store: could not read {path}: {exc}; using default")
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _zip_key(zip_code: Any) -> str:
    return str(zip_code or "").strip().zfill(5)


# --------------------------------------------------------------------------
# Lead collections ({zip: [record dicts]})
# --------------------------------------------------------------------------
def _load_leads(filename: str, base_dir: Optional[Path] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    data = _read_json(_ops_dir(base_dir) / filename, {})
    return data if isinstance(data, dict) else {}


def _save_leads(filename: str, data: Dict[str, List[Dict[str, Any]]],
                base_dir: Optional[Path] = None) -> None:
    _write_json_atomic(_ops_dir(base_dir) / filename, data)


def load_instructor_candidates(zip_code: Optional[str] = None,
                               base_dir: Optional[Path] = None
                               ) -> List[Dict[str, Any]]:
    data = _load_leads(INSTRUCTOR_FILE, base_dir)
    if zip_code is None:
        return [row for rows in data.values() for row in rows]
    return list(data.get(_zip_key(zip_code)) or [])


def load_space_candidates(zip_code: Optional[str] = None,
                          base_dir: Optional[Path] = None
                          ) -> List[Dict[str, Any]]:
    data = _load_leads(SPACE_FILE, base_dir)
    if zip_code is None:
        return [row for rows in data.values() for row in rows]
    return list(data.get(_zip_key(zip_code)) or [])


def save_zip_candidates(lead_type: str, zip_code: str,
                        candidates: List[Dict[str, Any]],
                        base_dir: Optional[Path] = None) -> None:
    """Replace the stored candidate list for one ZIP.

    Existing human CRM state (outreach_status, notes, verification fields) is
    preserved for candidates whose ``id`` — or (name, source) pair for
    re-discovered leads — already exists, so a refresh never silently undoes
    staff work.
    """
    filename = INSTRUCTOR_FILE if lead_type == "INSTRUCTOR" else SPACE_FILE
    data = _load_leads(filename, base_dir)
    key = _zip_key(zip_code)
    existing = {(_lead_identity(row)): row for row in data.get(key) or []}
    merged: List[Dict[str, Any]] = []
    for cand in candidates:
        prior = existing.get(_lead_identity(cand))
        if prior:
            cand = _merge_preserving_crm(prior, cand)
        merged.append(cand)
    # Keep leads staff already touched even if discovery no longer emits them.
    merged_ids = {_lead_identity(row) for row in merged}
    for identity, prior in existing.items():
        if identity not in merged_ids and prior.get("outreach_status") not in (
                None, "", "NEW"):
            merged.append(prior)
    data[key] = merged
    _save_leads(filename, data, base_dir)


def add_zip_candidates(lead_type: str, zip_code: str,
                       candidates: List[Dict[str, Any]],
                       base_dir: Optional[Path] = None) -> int:
    """Merge new candidates into a ZIP's stored list (adds, never wipes).

    Unlike ``save_zip_candidates`` (which replaces the ZIP's list), this keeps
    every already-stored lead and folds the incoming ones in, so a live source
    like AHA Atlas can add leads alongside offline discovery without undoing it.
    Existing leads with the same identity (id, or name|source) are updated
    while their CRM state is preserved. Returns how many incoming candidates
    were genuinely new.
    """
    filename = INSTRUCTOR_FILE if lead_type == "INSTRUCTOR" else SPACE_FILE
    data = _load_leads(filename, base_dir)
    key = _zip_key(zip_code)
    existing = list(data.get(key) or [])
    by_identity = {_lead_identity(row): row for row in existing}
    added = 0
    for cand in candidates:
        identity = _lead_identity(cand)
        prior = by_identity.get(identity)
        if prior:
            merged = _merge_preserving_crm(prior, cand)
            prior.clear()
            prior.update(merged)
        else:
            existing.append(cand)
            by_identity[identity] = cand
            added += 1
    data[key] = existing
    _save_leads(filename, data, base_dir)
    return added


_CRM_FIELDS = (
    "outreach_status", "notes", "verified_certifications", "credential_status",
    "long_term_interest", "availability_notes", "rate_notes", "email", "phone",
    "travel_radius_miles", "hourly_rate", "daily_rate", "capacity",
    "movable_tables_chairs", "weekend_available", "evening_available",
    "recurring_available", "wifi", "restroom_access", "ada_access",
    "camera_allowed", "access_control_possible", "signage_allowed",
    "training_use_allowed", "cancellation_policy", "created_at",
)


def _lead_identity(row: Dict[str, Any]) -> str:
    rid = str(row.get("id") or "")
    if rid:
        return rid
    return f"{row.get('name', '')}|{row.get('source', '')}"


def _merge_preserving_crm(prior: Dict[str, Any],
                          fresh: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(fresh)
    out["id"] = prior.get("id") or fresh.get("id")
    for fld in _CRM_FIELDS:
        prior_val = prior.get(fld)
        if prior_val not in (None, "", [], "NEW", "UNKNOWN"):
            out[fld] = prior_val
    return out


def find_lead(lead_type: str, lead_id: str, base_dir: Optional[Path] = None
              ) -> Optional[Dict[str, Any]]:
    filename = INSTRUCTOR_FILE if lead_type == "INSTRUCTOR" else SPACE_FILE
    data = _load_leads(filename, base_dir)
    for rows in data.values():
        for row in rows:
            if row.get("id") == lead_id:
                return row
    return None


def update_lead(lead_type: str, lead_id: str, updates: Dict[str, Any],
                base_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Apply field updates to one stored lead; returns the updated record."""
    filename = INSTRUCTOR_FILE if lead_type == "INSTRUCTOR" else SPACE_FILE
    data = _load_leads(filename, base_dir)
    for rows in data.values():
        for row in rows:
            if row.get("id") == lead_id:
                row.update(updates)
                row["updated_at"] = utc_now_iso()
                _save_leads(filename, data, base_dir)
                return row
    return None


# --------------------------------------------------------------------------
# Outreach log
# --------------------------------------------------------------------------
def load_outreach_log(target_id: Optional[str] = None,
                      base_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    data = _read_json(_ops_dir(base_dir) / OUTREACH_LOG_FILE, [])
    if not isinstance(data, list):
        return []
    if target_id is None:
        return data
    return [row for row in data if row.get("target_id") == target_id]


def append_outreach_log(entry: Dict[str, Any],
                        base_dir: Optional[Path] = None) -> Dict[str, Any]:
    data = load_outreach_log(base_dir=base_dir)
    data.append(entry)
    _write_json_atomic(_ops_dir(base_dir) / OUTREACH_LOG_FILE, data)
    return entry


# --------------------------------------------------------------------------
# Refresh gate
# --------------------------------------------------------------------------
def last_refresh_at(zip_code: str, base_dir: Optional[Path] = None
                    ) -> Optional[str]:
    state = _read_json(_ops_dir(base_dir) / REFRESH_STATE_FILE, {})
    if not isinstance(state, dict):
        return None
    return state.get(_zip_key(zip_code))


def refresh_allowed(zip_code: str, base_dir: Optional[Path] = None) -> bool:
    """True when the per-ZIP refresh cool-down has elapsed."""
    stamp = last_refresh_at(zip_code, base_dir)
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - then).total_seconds()
    return age >= REFRESH_MIN_INTERVAL_SECONDS


def mark_refreshed(zip_code: str, base_dir: Optional[Path] = None) -> str:
    state = _read_json(_ops_dir(base_dir) / REFRESH_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    stamp = utc_now_iso()
    state[_zip_key(zip_code)] = stamp
    _write_json_atomic(_ops_dir(base_dir) / REFRESH_STATE_FILE, state)
    return stamp


# --------------------------------------------------------------------------
# Manatal job registry — one position per location, with a creation rate cap
# --------------------------------------------------------------------------
# Pushing an instructor opens (or reuses) an "AHA Instructor" job at that
# instructor's location. The registry is what makes the push idempotent per
# location and what backs the daily creation cap, so the connector can never
# spam Manatal with duplicate positions and get the account flagged.
MANATAL_JOBS_FILE = "manatal_jobs.json"
_MANATAL_CREATED_LOG_MAX = 500


def manatal_job_location_key(city: str = "", state: str = "",
                             zip_code: str = "") -> str:
    """Stable dedupe key for a job location (city|state, else the ZIP)."""
    city = str(city or "").strip().lower()
    state = str(state or "").strip().lower()
    if city or state:
        return f"{city}|{state}"
    return f"zip|{_zip_key(zip_code)}"


def load_manatal_jobs(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    data = _read_json(_ops_dir(base_dir) / MANATAL_JOBS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("jobs"), dict):
        data["jobs"] = {}
    if not isinstance(data.get("created_log"), list):
        data["created_log"] = []
    return data


def find_manatal_job(location_key: str, base_dir: Optional[Path] = None
                     ) -> Optional[Dict[str, Any]]:
    job = load_manatal_jobs(base_dir)["jobs"].get(str(location_key))
    return job if isinstance(job, dict) else None


def record_manatal_job(location_key: str, record: Dict[str, Any],
                       base_dir: Optional[Path] = None) -> None:
    data = load_manatal_jobs(base_dir)
    data["jobs"][str(location_key)] = {
        **record, "created_at": record.get("created_at") or utc_now_iso()}
    data["created_log"] = (
        data["created_log"] + [utc_now_iso()])[-_MANATAL_CREATED_LOG_MAX:]
    _write_json_atomic(_ops_dir(base_dir) / MANATAL_JOBS_FILE, data)


def manatal_jobs_created_since(hours: float = 24.0,
                               base_dir: Optional[Path] = None) -> int:
    """How many Manatal jobs this instance created in the last N hours."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600.0
    count = 0
    for stamp in load_manatal_jobs(base_dir)["created_log"]:
        try:
            then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        if then.timestamp() >= cutoff:
            count += 1
    return count


# --------------------------------------------------------------------------
# Whole-store import (hosted deployments)
# --------------------------------------------------------------------------
def import_store_payload(payload: Dict[str, Any], mode: str = "merge",
                         base_dir: Optional[Path] = None) -> Dict[str, int]:
    """Import a full ops-store snapshot (the four JSON files as one payload).

    Used by the authenticated /api/ops/admin/import-store endpoint so the
    real (gitignored) store built locally can be pushed to a hosted instance.

    mode="merge" (default) routes candidates through save_zip_candidates so
    CRM state staff already entered on the hosted instance is preserved;
    outreach-log entries are appended if their id is not present yet.
    mode="replace" overwrites each file with the uploaded contents.
    """
    if mode not in ("merge", "replace"):
        raise ValueError(f"unknown import mode: {mode!r}")

    counts = {"instructor_candidates": 0, "space_candidates": 0,
              "outreach_log": 0, "refresh_state": 0}

    for lead_type, key, filename in (
            ("INSTRUCTOR", "instructor_candidates", INSTRUCTOR_FILE),
            ("SPACE", "space_candidates", SPACE_FILE)):
        incoming = payload.get(key)
        if not isinstance(incoming, dict):
            continue
        if mode == "replace":
            cleaned = {_zip_key(z): list(rows) for z, rows in incoming.items()
                       if isinstance(rows, list)}
            _save_leads(filename, cleaned, base_dir)
            counts[key] = sum(len(rows) for rows in cleaned.values())
        else:
            for zip_code, rows in incoming.items():
                if not isinstance(rows, list):
                    continue
                save_zip_candidates(lead_type, zip_code, rows, base_dir)
                counts[key] += len(rows)

    incoming_log = payload.get("outreach_log")
    if isinstance(incoming_log, list):
        if mode == "replace":
            log = list(incoming_log)
        else:
            log = load_outreach_log(base_dir=base_dir)
            seen = {row.get("id") for row in log if row.get("id")}
            log.extend(row for row in incoming_log
                       if not row.get("id") or row.get("id") not in seen)
        _write_json_atomic(_ops_dir(base_dir) / OUTREACH_LOG_FILE, log)
        counts["outreach_log"] = len(log)

    incoming_state = payload.get("refresh_state")
    if isinstance(incoming_state, dict):
        state = {} if mode == "replace" else _read_json(
            _ops_dir(base_dir) / REFRESH_STATE_FILE, {})
        if not isinstance(state, dict):
            state = {}
        state.update({_zip_key(z): ts for z, ts in incoming_state.items()})
        _write_json_atomic(_ops_dir(base_dir) / REFRESH_STATE_FILE, state)
        counts["refresh_state"] = len(state)

    logger.info(f"ops store import ({mode}): {counts}")
    return counts
