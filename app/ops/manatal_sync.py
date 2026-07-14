"""
Manatal ATS sync — turn ALLCPR instructor leads into recruiting pipeline.

Flow: the engine finds a lead → push it to Manatal as a candidate (attached to a
per-ZIP/course job) → Manatal tracks recruiting → pull the stage back → the
lead's CRM status + instructor readiness update. This is the loop that makes
professor/instructor hunting into managed recruiting.

Everything here is gated by ``manatal_client`` (off by default) and never
touches the secret token. Stage mapping and payload/note building are pure
functions so they can be tested without any network.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from app.ops import store
from app.ops.manatal_client import ManatalClient, status as manatal_status
from app.ops.models import COURSE_LABELS, utc_now_iso

# --------------------------------------------------------------------------
# Stage mapping: Manatal stage/status  →  internal CRM + readiness
# --------------------------------------------------------------------------
# Updates applied to the stored lead when a Manatal stage is seen. outreach_
# status values are from INSTRUCTOR_OUTREACH_STATUSES; credential_status is set
# only where the stage implies it (verified/rejected).
_STAGE_TO_UPDATES = {
    "new": {"outreach_status": "NEEDS_REVIEW"},
    "sourced": {"outreach_status": "NEEDS_REVIEW"},
    "contacted": {"outreach_status": "CONTACTED"},
    "replied": {"outreach_status": "REPLIED"},
    "interested": {"outreach_status": "INTERESTED"},
    "credential requested": {"outreach_status": "NEEDS_REVIEW",
                             "credential_status": "NEEDS_VERIFICATION"},
    "credential verified": {"outreach_status": "CREDENTIAL_VERIFIED",
                            "credential_status": "VERIFIED"},
    "rate confirmed": {"outreach_status": "AVAILABLE"},
    "ready": {"outreach_status": "CONFIRMED"},
    "ready to teach": {"outreach_status": "CONFIRMED"},
    "hired": {"outreach_status": "CONFIRMED"},
    "approved": {"outreach_status": "CONFIRMED"},
    "rejected": {"outreach_status": "REJECTED",
                 "credential_status": "REJECTED"},
    "not interested": {"outreach_status": "NOT_INTERESTED"},
}

# Instructor-readiness contribution per stage (the recruiting ladder).
_STAGE_READINESS = {
    "contacted": 50.0, "replied": 55.0, "interested": 65.0,
    "credential requested": 70.0, "credential verified": 85.0,
    "rate confirmed": 90.0, "ready": 100.0, "ready to teach": 100.0,
    "hired": 100.0, "approved": 100.0, "rejected": 0.0, "not interested": 0.0,
}


# Keyword fallback so common / renamed Manatal stages still map even when they
# don't exactly match the table above. Ordered most-advanced first; first
# substring hit wins (so "credential verified" beats bare "verified").
_STAGE_KEYWORDS = (
    ("hired", {"outreach_status": "CONFIRMED"}),
    ("placed", {"outreach_status": "CONFIRMED"}),
    ("ready", {"outreach_status": "CONFIRMED"}),
    ("approved", {"outreach_status": "CONFIRMED"}),
    ("rate confirmed", {"outreach_status": "AVAILABLE"}),
    ("credential verified", {"outreach_status": "CREDENTIAL_VERIFIED",
                             "credential_status": "VERIFIED"}),
    ("verified", {"outreach_status": "CREDENTIAL_VERIFIED",
                  "credential_status": "VERIFIED"}),
    ("credential", {"outreach_status": "NEEDS_REVIEW",
                    "credential_status": "NEEDS_VERIFICATION"}),
    ("offer", {"outreach_status": "AVAILABLE"}),
    ("interview", {"outreach_status": "INTERESTED"}),
    ("interested", {"outreach_status": "INTERESTED"}),
    ("assessment", {"outreach_status": "INTERESTED"}),
    ("shortlist", {"outreach_status": "INTERESTED"}),
    ("replied", {"outreach_status": "REPLIED"}),
    ("screening", {"outreach_status": "CONTACTED"}),
    ("screen", {"outreach_status": "CONTACTED"}),
    ("contacted", {"outreach_status": "CONTACTED"}),
    ("not interested", {"outreach_status": "NOT_INTERESTED"}),
    ("rejected", {"outreach_status": "REJECTED",
                  "credential_status": "REJECTED"}),
    ("declined", {"outreach_status": "REJECTED"}),
    ("disqualif", {"outreach_status": "REJECTED"}),
    ("dropped", {"outreach_status": "REJECTED"}),
    ("new", {"outreach_status": "NEEDS_REVIEW"}),
    ("sourced", {"outreach_status": "NEEDS_REVIEW"}),
    ("applied", {"outreach_status": "NEEDS_REVIEW"}),
)

# Readiness score per resolved internal status (used when the raw stage isn't
# in _STAGE_READINESS but maps to a status via keywords).
_STATUS_READINESS = {
    "CONTACTED": 50.0, "REPLIED": 55.0, "INTERESTED": 65.0,
    "CREDENTIAL_VERIFIED": 85.0, "AVAILABLE": 90.0, "CONFIRMED": 100.0,
    "NOT_INTERESTED": 0.0, "REJECTED": 0.0,
}


def _norm_stage(stage: Any) -> str:
    return re.sub(r"[\s_\-]+", " ", str(stage or "").strip().lower())


def map_stage(stage: Any) -> Dict[str, str]:
    """Manatal stage → CRM field updates ({} when nothing matches).

    Exact table first, then a keyword fallback so custom/renamed stages still
    resolve (e.g. "Interview" → INTERESTED, "Hired" → CONFIRMED).
    """
    s = _norm_stage(stage)
    if s in _STAGE_TO_UPDATES:
        return dict(_STAGE_TO_UPDATES[s])
    for kw, updates in _STAGE_KEYWORDS:
        if kw in s:
            return dict(updates)
    return {}


def readiness_from_stage(stage: Any) -> Optional[float]:
    """0..100 instructor-readiness contribution for a stage (None if neutral)."""
    s = _norm_stage(stage)
    if s in _STAGE_READINESS:
        return _STAGE_READINESS[s]
    return _STATUS_READINESS.get(map_stage(stage).get("outreach_status"))


# --------------------------------------------------------------------------
# Payload / note builders (pure)
# --------------------------------------------------------------------------
_JOB_LABEL = {
    "AHA_BLS": "AHA BLS Instructor",
    "ARC_BLS": "Red Cross BLS Instructor",
    "ARC_CPR_FA_AED": "CPR/FA/AED Instructor",
    "OVERALL": "CPR/BLS Instructor Pool",
}


def job_name(course: str, city: str = "", zip_code: str = "") -> str:
    label = _JOB_LABEL.get(str(course).upper(), "CPR/BLS Instructor")
    where = f"{city} / {zip_code}" if city and zip_code else (
        f"ZIP {zip_code}" if zip_code else "")
    return f"{label} — {where}" if where else label


def build_job_payload(zip_code: str, course: str,
                      ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A Manatal job (Open API v3 fields) for a ZIP/course. No secrets.

    Manatal's POST /jobs/ expects position_name + a client organization id
    (MANATAL_DEFAULT_ORGANIZATION). The ALLCPR context goes in description.
    """
    ctx = ctx or {}
    course = str(course).upper()
    name = job_name(course, str(ctx.get("city") or ""), zip_code)
    why = ctx.get("why") or (
        f"{COURSE_LABELS.get(course, course)} demand in ZIP {zip_code} needs "
        "instructor supply.")
    extras = []
    if ctx.get("demand_score") is not None:
        extras.append(f"Demand score: {ctx['demand_score']}")
    if ctx.get("nearest_site"):
        extras.append(f"Nearest ALLCPR site: {ctx['nearest_site']}")
    payload: Dict[str, Any] = {
        "position_name": name,
        "description": " | ".join([why] + extras),
    }
    org = os.environ.get("MANATAL_DEFAULT_ORGANIZATION", "").strip()
    if org:
        payload["organization"] = org
    return payload


def build_candidate_payload(lead: Dict[str, Any], zip_code: str = "",
                            course: str = "") -> Dict[str, Any]:
    """A Manatal candidate (Open API v3 fields) from an instructor lead.

    Real fields only (full_name is the sole required one); the ALLCPR match
    context is written into ``description``. Never includes secrets.
    """
    full_name = str(lead.get("name") or "").strip()
    address = ", ".join(p for p in (
        str(lead.get("address") or ""), str(lead.get("city") or ""),
        str(lead.get("state") or "")) if p)
    # No source_type: Manatal validates it against an account-specific choice
    # list ("Other" 400s) — provenance goes in the description note instead.
    payload: Dict[str, Any] = {
        "full_name": full_name,
        "description": build_candidate_note(lead, zip_code=zip_code,
                                            course=course),
    }
    # Optional fields only when non-empty — Manatal's API 400s on blank
    # values for validated fields like email.
    for key, value in (("email", lead.get("email")),
                       ("phone_number", lead.get("phone")),
                       ("current_position", lead.get("title")),
                       ("current_company", lead.get("organization")),
                       ("address", address),
                       ("zipcode", str(lead.get("zip") or ""))):
        if value:
            payload[key] = value
    if lead.get("id"):
        payload["external_id"] = f"allcpr-{lead['id']}"
    owner = os.environ.get("MANATAL_DEFAULT_OWNER_ID", "").strip()
    if owner:
        payload["owner"] = owner
    return payload


def build_candidate_note(lead: Dict[str, Any], zip_code: str = "",
                         course: str = "",
                         ctx: Optional[Dict[str, Any]] = None) -> str:
    """The recruiting note attached to the candidate (human-readable, safe)."""
    ctx = ctx or {}
    course = str(course or "").upper()
    lines = [
        f"ALLCPR match — ZIP {zip_code or lead.get('zip') or '?'}",
        f"Course need: {COURSE_LABELS.get(course, course or 'CPR/BLS')}",
    ]
    if ctx.get("demand_score") is not None:
        lines.append(f"Demand score: {ctx['demand_score']}")
    if lead.get("past_instructor"):
        lines.append(f"PAST ALLCPR instructor — taught "
                     f"{lead.get('past_students') or '?'} students before "
                     f"(proven: {', '.join(lead.get('proven_courses') or []) or 'n/a'})")
    dist = lead.get("distance_miles")
    if dist is not None:
        lines.append(f"Distance to ZIP: {dist} mi")
    lines.append(f"Source: {lead.get('source_url') or lead.get('source') or 'ALLCPR engine'}")
    lines.append("Verify before booking: current AHA/ARC instructor "
                 "credential, availability, rate.")
    lines.append(f"Current dashboard status: {lead.get('outreach_status') or 'NEW'} "
                 f"/ {lead.get('credential_status') or 'UNKNOWN'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Instructor-position jobs (the recruiting team's real workflow)
# --------------------------------------------------------------------------
# The team runs one "AHA Instructor" job per location; pushing an instructor
# must open (or reuse) the position at THAT instructor's location, with the
# professor's name in the note — never a flood of duplicate positions.
_DEFAULT_POSITION_NAME = "AHA Instructor"
_DEFAULT_JOB_DAILY_CAP = 5

# Resolved organization id, cached per process (jobs REQUIRE one and it never
# changes for an account).
_ORG_CACHE: Dict[str, str] = {}


def _job_env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def instructor_position_name() -> str:
    return _job_env("MANATAL_JOB_POSITION_NAME", _DEFAULT_POSITION_NAME)


def job_daily_cap() -> int:
    """Max Manatal jobs this instance may create per rolling 24h (0 = off)."""
    try:
        return max(0, int(_job_env("MANATAL_JOB_DAILY_CAP",
                                   str(_DEFAULT_JOB_DAILY_CAP))))
    except ValueError:
        return _DEFAULT_JOB_DAILY_CAP


def _lead_location(lead: Dict[str, Any],
                   ctx: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    ctx = ctx or {}
    return {"city": str(lead.get("city") or ctx.get("city") or "").strip(),
            "state": str(lead.get("state") or ctx.get("state") or "").strip()}


def build_instructor_job_payload(lead: Dict[str, Any], zip_code: str = "",
                                 course: str = "",
                                 ctx: Optional[Dict[str, Any]] = None
                                 ) -> Dict[str, Any]:
    """A Manatal job matching the team's manual convention: position name
    "AHA Instructor", located where the instructor is, 25–45 USD, headcount 2.

    ``organization`` (required by Manatal) is attached by the caller after
    resolution. Never includes secrets.
    """
    loc = _lead_location(lead, ctx)
    name = str(lead.get("name") or "").strip()
    header = f"Opened for instructor: {name}" if name else ""
    note = build_candidate_note(lead, zip_code=zip_code, course=course,
                                ctx=ctx)
    payload: Dict[str, Any] = {
        "position_name": instructor_position_name(),
        "description": f"{header}\n{note}" if header else note,
        "salary_min": _job_env("MANATAL_JOB_SALARY_MIN", "25"),
        "salary_max": _job_env("MANATAL_JOB_SALARY_MAX", "45"),
        "currency": _job_env("MANATAL_JOB_CURRENCY", "USD"),
        "status": "active",
    }
    try:
        payload["headcount"] = int(_job_env("MANATAL_JOB_HEADCOUNT", "2"))
    except ValueError:
        payload["headcount"] = 2
    if loc["city"]:
        payload["city"] = loc["city"]
    if loc["state"]:
        payload["state"] = loc["state"]
    if loc["city"] or loc["state"]:
        payload["country"] = _job_env("MANATAL_JOB_COUNTRY", "United States")
    zipcode = str(lead.get("zip") or zip_code or "").strip()
    if zipcode:
        payload["zipcode"] = zipcode
    owner = os.environ.get("MANATAL_DEFAULT_OWNER_ID", "").strip()
    if owner:
        payload["owner"] = owner
    return payload


def _results(res: Dict[str, Any]) -> list:
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    rows = data.get("results")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) \
        else []


def _job_defaults(client: ManatalClient) -> Dict[str, str]:
    """organization + owner for new records. Env vars win; otherwise both are
    COPIED from the team's existing jobs (sampled once per process — new
    positions look exactly like the manually created ones); as a last resort
    the account's single client organization is used. Empty string = unknown.
    """
    org = os.environ.get("MANATAL_DEFAULT_ORGANIZATION", "").strip()
    owner = os.environ.get("MANATAL_DEFAULT_OWNER_ID", "").strip()
    if (org or _ORG_CACHE.get("org")) and (owner or _ORG_CACHE.get("owner")):
        return {"org": org or _ORG_CACHE.get("org", ""),
                "owner": owner or _ORG_CACHE.get("owner", "")}
    res = client.list_jobs(limit=5)
    if res.get("ok") and not res.get("dry_run"):
        rows = _results(res)
        orgs = {str(r.get("organization")) for r in rows
                if r.get("organization") not in (None, "")}
        owners = [str(r.get("owner")) for r in rows
                  if r.get("owner") not in (None, "")]
        if len(orgs) == 1:
            _ORG_CACHE["org"] = next(iter(orgs))
        if owners:
            _ORG_CACHE["owner"] = max(set(owners), key=owners.count)
    if not (org or _ORG_CACHE.get("org")):
        res = client.list_organizations()
        if res.get("ok") and not res.get("dry_run"):
            rows = _results(res)
            if len(rows) == 1 and rows[0].get("id") not in (None, ""):
                _ORG_CACHE["org"] = str(rows[0]["id"])
    return {"org": org or _ORG_CACHE.get("org", ""),
            "owner": owner or _ORG_CACHE.get("owner", "")}


def find_or_create_instructor_job(lead: Dict[str, Any], zip_code: str = "",
                                  course: str = "",
                                  ctx: Optional[Dict[str, Any]] = None,
                                  client: Optional[ManatalClient] = None
                                  ) -> Dict[str, Any]:
    """The job an instructor push attaches to — reused per location.

    Reuse (registry hit) adds a note with the professor's name to the existing
    position. Creation is capped per 24h so the account is never flagged for
    opening too many positions.
    """
    client = client or ManatalClient()
    loc = _lead_location(lead, ctx)
    loc_key = store.manatal_job_location_key(
        loc["city"], loc["state"], str(lead.get("zip") or zip_code or ""))
    existing = store.find_manatal_job(loc_key)
    if existing and existing.get("job_id"):
        job_id = str(existing["job_id"])
        defaults = _job_defaults(client)
        name = str(lead.get("name") or "").strip()
        if name:
            # Best-effort — the reused position still records who was added.
            client.add_job_note(job_id, (
                f"ALLCPR push: instructor {name} added to this position "
                f"(ZIP {zip_code or lead.get('zip') or '?'})."))
        return {"ok": True, "manatal_job_id": job_id, "job_reused": True,
                "position_name": (existing.get("position_name")
                                  or instructor_position_name()),
                "owner": defaults["owner"]}

    cap = job_daily_cap()
    if cap and store.manatal_jobs_created_since(24.0) >= cap:
        return {"ok": False, "error": "job_cap_reached",
                "message": (f"Daily Manatal job-creation cap reached "
                            f"({cap}/24h) — not opening another position, to "
                            "avoid the account being flagged. Retry later or "
                            "raise MANATAL_JOB_DAILY_CAP.")}

    payload = build_instructor_job_payload(lead, zip_code=zip_code,
                                           course=course, ctx=ctx)
    from app.ops.manatal_client import write_enabled  # noqa: PLC0415
    defaults = _job_defaults(client)
    if defaults["org"]:
        payload["organization"] = defaults["org"]
    elif write_enabled():
        # Manatal rejects jobs without an organization — fail with a real fix
        # instead of burning a doomed API call.
        return {"ok": False, "error": "organization_required",
                "message": ("Manatal requires an organization id to create a "
                            "job and none could be resolved from existing "
                            "jobs. Set MANATAL_DEFAULT_ORGANIZATION to your "
                            "client organization id (Manatal → "
                            "Organizations).")}
    if defaults["owner"] and "owner" not in payload:
        payload["owner"] = defaults["owner"]
    created = client.create_job(payload)
    if created.get("dry_run"):
        created["job_reused"] = False
        return created
    if not created.get("ok"):
        return created
    job_id = _extract_id(created.get("data"))
    if not job_id:
        return {"ok": False, "error": "job_id_missing",
                "message": "Manatal created the job but returned no id."}
    store.record_manatal_job(loc_key, {
        "job_id": str(job_id), "position_name": payload["position_name"],
        "city": loc["city"], "state": loc["state"],
        "zip": str(lead.get("zip") or zip_code or ""),
        "opened_for": str(lead.get("name") or "")})
    return {"ok": True, "manatal_job_id": str(job_id), "job_reused": False,
            "position_name": payload["position_name"],
            "owner": defaults["owner"]}


# --------------------------------------------------------------------------
# Response parsing (defensive — Manatal schema may vary by API version)
# --------------------------------------------------------------------------
def _extract_id(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        for k in ("id", "candidate_id", "pk"):
            if data.get(k) not in (None, ""):
                return str(data[k])
        for k in ("candidate", "data", "result"):
            got = _extract_id(data.get(k))
            if got:
                return got
    return None


def extract_stage(data: Any) -> str:
    # Unwrap a matches collection (GET /candidates/{id}/matches/): the stage
    # lives on the (most recent) match, not the candidate itself.
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"][0] if data["results"] else {}
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    for k in ("stage", "status", "pipeline_stage", "recruitment_status",
              "current_stage", "match_stage"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            nm = v.get("name") or v.get("label") or v.get("title")
            if nm:
                return str(nm)
    for k in ("recruitment", "pipeline", "match", "data"):
        v = data.get(k)
        if isinstance(v, dict):
            got = extract_stage(v)
            if got:
                return got
    return ""


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def create_job_for_zip(zip_code: str, course: str = "OVERALL",
                       ctx: Optional[Dict[str, Any]] = None,
                       client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    client = client or ManatalClient()
    payload = build_job_payload(zip_code, course, ctx)
    result = client.create_job(payload)
    result["job_name"] = payload["position_name"]
    return result


def _find_instructor_by_manatal_id(candidate_id: str
                                   ) -> Optional[Dict[str, Any]]:
    for lead in store.load_instructor_candidates():
        if str(lead.get("manatal_candidate_id") or "") == str(candidate_id):
            return lead
    return None


def delete_candidate(candidate_id: str,
                     client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    """Delete a linked Manatal candidate and clear the local sync mapping."""
    lead = _find_instructor_by_manatal_id(candidate_id)
    if not lead:
        return {"ok": False, "error": "linked_lead_not_found",
                "candidate_id": str(candidate_id)}
    client = client or ManatalClient()
    result = client.delete_candidate(str(candidate_id))
    if not result.get("ok") or result.get("dry_run"):
        return result
    store.update_lead("INSTRUCTOR", str(lead.get("id") or ""), {
        "manatal_candidate_id": None,
        "manatal_job_id": None,
        "manatal_stage": None,
        "last_synced_at": None,
    })
    return {"ok": True, "candidate_id": str(candidate_id),
            "lead_id": str(lead.get("id") or ""),
            "deleted": True}


def push_lead(lead_id: str, zip_code: str = "", course: str = "",
              job_id: str = "", ctx: Optional[Dict[str, Any]] = None,
              client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    """Push one instructor lead to Manatal: open (or reuse) the "AHA
    Instructor" position at the lead's location, then add the professor as a
    candidate matched into it, with their name in the notes.

    Pass ``job_id`` to skip position lookup and attach to a known job. If the
    position can't be opened (daily cap, missing organization, API error),
    NOTHING is pushed — no orphan candidates.
    """
    client = client or ManatalClient()
    lead = store.find_lead("INSTRUCTOR", lead_id)
    if not lead:
        return {"ok": False, "error": "lead_not_found", "lead_id": lead_id}
    zip_code = zip_code or str(lead.get("zip") or "")
    job_info: Dict[str, Any] = {}
    job_owner = ""
    if not job_id:
        job_res = find_or_create_instructor_job(
            lead, zip_code=zip_code, course=course, ctx=ctx, client=client)
        if job_res.get("dry_run"):
            job_info = {"job_dry_run": True,
                        "job_would_send": job_res.get("would_send")}
        elif not job_res.get("ok"):
            return job_res      # cap / org missing / API error — push nothing
        else:
            job_id = str(job_res.get("manatal_job_id") or "")
            job_owner = str(job_res.get("owner") or "")
            job_info = {"manatal_job_id": job_id,
                        "job_reused": bool(job_res.get("job_reused")),
                        "position_name": job_res.get("position_name")}
    payload = build_candidate_payload(lead, zip_code=zip_code, course=course)
    if job_owner and "owner" not in payload:
        # Same owner as the team's jobs — so the candidate shows up in their
        # default (owner-filtered) Manatal views.
        payload["owner"] = job_owner
    note = build_candidate_note(lead, zip_code=zip_code, course=course)
    result = client.create_candidate(payload)
    if not result.get("ok"):
        result.update(job_info)
        return result           # disabled / not_configured / error
    if result.get("dry_run"):
        result["note_preview"] = note
        result.update(job_info)
        return result
    result.update(job_info)
    candidate_id = _extract_id(result.get("data"))
    updates: Dict[str, Any] = {}
    if candidate_id:
        client.add_candidate_note(candidate_id, note)
        updates["manatal_candidate_id"] = candidate_id
        result["manatal_candidate_id"] = candidate_id
        if job_id:
            match = client.create_match(candidate_id, job_id)
            if match.get("ok"):
                updates["manatal_job_id"] = job_id
        updates["last_synced_at"] = utc_now_iso()
        store.update_lead("INSTRUCTOR", lead_id, updates)
    return result


def push_top_leads(zip_code: str, limit: int = 5, course: str = "",
                   client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    """Push the ZIP's top named instructor leads (with a store id) to Manatal."""
    from app.ops.instructor_matching import match_instructors_for_zip  # noqa: PLC0415
    client = client or ManatalClient()
    match = match_instructors_for_zip(zip_code, course=course or None,
                                      limit=max(1, min(30, limit * 3)))
    pushed, skipped = [], []
    for lead in match["best_instructor_path"]:
        # Only real, named, stored leads (level >= 2 with an id) are pushable.
        if not lead.get("id") or (lead.get("lead_level") or 0) < 2:
            skipped.append({"name": lead.get("name"),
                            "reason": "signal_or_unsaved"})
            continue
        if len(pushed) >= limit:
            break
        res = push_lead(str(lead["id"]), zip_code=zip_code, course=course,
                        client=client)
        pushed.append({"name": lead.get("name"), "ok": res.get("ok"),
                       "dry_run": res.get("dry_run", False),
                       "manatal_candidate_id": res.get("manatal_candidate_id")})
        if res.get("disabled"):
            return {"ok": False, "disabled": True, "mode": "DISABLED",
                    "message": res.get("message"), "attempted": pushed}
    return {"ok": True, "mode": manatal_status()["mode"], "zip": zip_code,
            "pushed": pushed, "skipped": skipped}


def sync_candidate(candidate_id: str,
                   client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    """Pull a candidate's stage from Manatal and update the linked lead."""
    client = client or ManatalClient()
    # The recruitment stage lives on the candidate's job match(es).
    result = client.get_candidate_matches(candidate_id)
    if not result.get("ok"):
        return result
    stage = extract_stage(result.get("data"))
    updates = map_stage(stage)
    lead = _find_instructor_by_manatal_id(candidate_id)
    applied = {}
    if lead and lead.get("id"):
        updates = {**updates, "manatal_stage": stage,
                   "last_synced_at": utc_now_iso()}
        store.update_lead("INSTRUCTOR", str(lead["id"]), updates)
        applied = updates
    return {"ok": True, "candidate_id": candidate_id, "manatal_stage": stage,
            "readiness_from_stage": readiness_from_stage(stage),
            "applied": applied}


def sync_zip(zip_code: str,
             client: Optional[ManatalClient] = None) -> Dict[str, Any]:
    """Sync every stored lead in a ZIP that is linked to a Manatal candidate."""
    client = client or ManatalClient()
    if manatal_status()["mode"] == "DISABLED":
        return {"ok": False, "disabled": True, "mode": "DISABLED",
                "message": ("Manatal integration disabled. Set MANATAL_ENABLED"
                            "=1 and credentials in the environment.")}
    synced = []
    for lead in store.load_instructor_candidates(zip_code):
        cid = str(lead.get("manatal_candidate_id") or "")
        if not cid:
            continue
        res = sync_candidate(cid, client=client)
        synced.append({"name": lead.get("name"), "candidate_id": cid,
                       "manatal_stage": res.get("manatal_stage"),
                       "ok": res.get("ok")})
    return {"ok": True, "mode": manatal_status()["mode"], "zip": zip_code,
            "synced": synced, "count": len(synced)}
