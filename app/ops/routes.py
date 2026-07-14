"""
FastAPI routes for the expansion-operations layer (``/api/ops/*``).

Mounted by ``web_app.py`` via ``app.include_router``. Every payload passes
through ``scrub_sensitive`` before it is serialized so staff-only operational
fields (access codes, lockbox codes, Wi-Fi passwords) can never leak through
this API even if they are ever entered into a manual import by mistake.

The refresh endpoint is offline/local (roster + manual CSVs + already-enriched
ZIP signals). It is still gated by a per-ZIP cool-down so wiring in live
sources later cannot turn it into a scraping hammer.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.ops import store
from app.ops.aha_atlas import aha_instructor_candidates
from app.ops.instructor_matching import match_instructors_for_zip
from app.ops.instructor_supply import discover_instructor_candidates
from app.ops.security import require_write_token, write_protection_status
from app.ops.models import (
    COURSE_TYPES,
    INSTRUCTOR_OUTREACH_STATUSES,
    OUTREACH_TARGET_TYPES,
    SPACE_OUTREACH_STATUSES,
    scrub_sensitive,
)
from app.ops.instructor_performance import load_instructor_performance, top_performers
from app.ops.instructor_sourcing import CREDENTIAL_REQUIREMENTS, build_sourcing_plan
from app.ops.local_market import local_market_context
from app.ops.manatal_client import status as manatal_status
from app.ops import manatal_sync
from app.ops.coverage import existing_site_coverage
from app.ops.revenue_health import nearest_site_health, revenue_health_summary
from app.ops.action_queue import build_action_queue
from app.ops.operating_feasibility import build_zip_operating_readiness
from app.ops.unit_economics import site_economics
from app.ops.outreach_templates import (
    build_outreach_log_entry,
    generate_instructor_outreach,
    generate_space_outreach,
)
from app.ops.outreach_engine import (
    approve_and_send,
    cancel_sequence,
    edit_queued_draft,
    engine_status,
    enqueue_lead,
    queue_snapshot,
    run_tick,
)
from app.ops.search_queries import generate_search_queries
from app.ops.space_supply import discover_space_candidates
from app.reports.commercial_validation import load_commercial_validation
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/ops", tags=["operating-readiness"])

_INVALID_ZIP = {"error": "invalid_zip", "message": "ZIP must be a 5-digit code."}

_LEAD_TYPES = {"instructor": "INSTRUCTOR", "space": "SPACE"}


def _valid_zip(zip_code: str) -> Optional[str]:
    zip_code = str(zip_code).strip()
    if len(zip_code) == 5 and zip_code.isdigit():
        return zip_code
    return None


def _load_zip_row(zip_code: str) -> Dict[str, Any]:
    """Reuse the dashboard's ZIP-detail loader; empty dict when unmodeled.

    Imported lazily because ``web_app`` imports this module at startup.
    """
    import web_app  # noqa: PLC0415 — deferred to avoid circular import

    return web_app._load_zip_detail(zip_code) or {}


def _refresh_zip_leads(zip_code: str) -> Dict[str, Any]:
    """Re-run offline discovery for one ZIP and persist, keeping CRM state."""
    zip_row = _load_zip_row(zip_code)
    commercial = load_commercial_validation().get(zip_code) or []
    instructors = discover_instructor_candidates(zip_code, zip_row=zip_row)
    spaces = discover_space_candidates(zip_code, zip_row=zip_row,
                                       commercial_rows=commercial)
    store.save_zip_candidates("INSTRUCTOR", zip_code, instructors)
    store.save_zip_candidates("SPACE", zip_code, spaces)
    stamp = store.mark_refreshed(zip_code)
    return {
        "zip": zip_code,
        "refreshed_at": stamp,
        "instructor_leads": len(store.load_instructor_candidates(zip_code)),
        "space_leads": len(store.load_space_candidates(zip_code)),
        "zip_row_found": bool(zip_row),
    }


def _leads_for_zip(zip_code: str) -> tuple:
    """Stored leads for a ZIP; discover once (and persist) when empty."""
    instructors = store.load_instructor_candidates(zip_code)
    spaces = store.load_space_candidates(zip_code)
    if not instructors and not spaces and store.refresh_allowed(zip_code):
        _refresh_zip_leads(zip_code)
        instructors = store.load_instructor_candidates(zip_code)
        spaces = store.load_space_candidates(zip_code)
    return instructors, spaces


@router.get("/zip/{zip_code}/readiness")
def api_readiness(zip_code: str) -> JSONResponse:
    """Combined operating readiness for a ZIP (all course types + leads)."""
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    zip_row = _load_zip_row(zip_code)
    instructors, spaces = _leads_for_zip(zip_code)
    payload = build_zip_operating_readiness(zip_code, zip_row, instructors,
                                            spaces)
    payload["last_lead_refresh_at"] = store.last_refresh_at(zip_code)
    return JSONResponse(scrub_sensitive(payload))


@router.get("/zip/{zip_code}/recommendation")
def api_recommendation(zip_code: str) -> JSONResponse:
    """Just the operator answer: action, explanation, gaps, next steps."""
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    zip_row = _load_zip_row(zip_code)
    instructors, spaces = _leads_for_zip(zip_code)
    payload = build_zip_operating_readiness(zip_code, zip_row, instructors,
                                            spaces)
    summary = payload["summary"]
    return JSONResponse(scrub_sensitive({
        "zip": zip_code,
        "recommended_action": summary["recommended_action"],
        "recommended_action_label": summary["recommended_action_label"],
        "explanation": summary["explanation"],
        "missing_requirements": summary["missing_requirements"],
        "risk_flags": summary["risk_flags"],
        "next_steps": summary["next_steps"],
        "operating_feasibility_score":
            summary["operating_feasibility_score"],
    }))


@router.get("/zip/{zip_code}/instructor-leads")
def api_instructor_leads(zip_code: str) -> JSONResponse:
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    instructors, _ = _leads_for_zip(zip_code)
    return JSONResponse(scrub_sensitive({
        "zip": zip_code,
        "count": len(instructors),
        "leads": instructors,
    }))


@router.get("/zip/{zip_code}/instructor-match")
def api_instructor_match(zip_code: str, course: str = "",
                         limit: int = 8,
                         radius: float = 25.0) -> JSONResponse:
    """Ranked 'best instructor path' for ANY US ZIP: past ALLCPR/Enrollware
    instructors (proven track record) first, then named leads, then signals —
    with distance, level (1..6), and the recommended next action. If nothing
    is within ``radius`` miles, the nearest past instructors are returned
    anyway with their true distance (expanded_search=true).
    """
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    course_filter = str(course or "").upper() or None
    if course_filter and course_filter not in COURSE_TYPES:
        return JSONResponse(
            {"error": "invalid_course",
             "message": f"course must be one of: {', '.join(COURSE_TYPES)}"},
            status_code=400,
        )
    try:
        lim = max(1, min(30, int(limit)))
    except (TypeError, ValueError):
        lim = 8
    try:
        rad = max(5.0, min(200.0, float(radius)))
    except (TypeError, ValueError):
        rad = 25.0
    # Ensure the ZIP has leads to match against (discover once if empty).
    _leads_for_zip(zip_code)
    result = match_instructors_for_zip(zip_code, course=course_filter,
                                       limit=lim, radius_miles=rad)
    return JSONResponse(scrub_sensitive(result))


@router.get("/zip/{zip_code}/aha-instructors")
def api_aha_instructors(zip_code: str, radius: float = 25.0,
                        limit: int = 25) -> JSONResponse:
    """Live AHA Atlas training-center leads near a ZIP (phone/email/address).

    Fetches AHA-registered Training Centers/Sites, keeps the contactable ones
    without their own website, and **persists them into the instructor store**
    so they immediately flow into the best-instructor path, outreach drafting,
    and Manatal push — the same pipeline as every other lead. Returns the
    stored leads. This is the one live external source (network call to AHA);
    it is env-gated and short-TTL cached.
    """
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    try:
        rad = max(5.0, min(100.0, float(radius)))
    except (TypeError, ValueError):
        rad = 25.0
    try:
        lim = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        lim = 25
    result = aha_instructor_candidates(zip_code, radius_miles=rad, limit=lim)
    added = 0
    if result.get("ok") and result.get("candidates"):
        added = store.add_zip_candidates("INSTRUCTOR", zip_code,
                                         result["candidates"])
    # Return the persisted AHA leads for this ZIP (with any ids the store
    # assigned/merged), so the dashboard can draft/queue them directly.
    stored_aha = [c for c in store.load_instructor_candidates(zip_code)
                  if c.get("source") == "aha_atlas"]
    stored_aha.sort(key=lambda c: (c.get("distance_miles") is None,
                                   c.get("distance_miles") or 9999.0))
    return JSONResponse(scrub_sensitive({
        "zip": zip_code,
        "ok": result.get("ok", False),
        "radius_miles": rad,
        "fetched": result.get("fetched", 0),
        "kept": result.get("kept", 0),
        "added": added,
        "count": len(stored_aha),
        "leads": stored_aha,
        "note": result.get("note", ""),
    }))


@router.post("/zip/{zip_code}/aha-instructors/draft-all")
def api_aha_draft_all(zip_code: str,
                      payload: Dict[str, Any] = Body(default={})
                      ) -> JSONResponse:
    """Auto-draft outreach for every stored AHA lead in a ZIP that has an email.

    Each lead's touch-1 draft is generated and placed in the approval queue
    (status PENDING_APPROVAL). Nothing is sent — the existing approve step
    (write-token gated) still gates every outbound email. This is the "automate
    the draft" one-click: fetch → draft → queue, stopping at the human send gate.
    """
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    try:
        limit = max(1, min(50, int(payload.get("limit") or 25)))
    except (TypeError, ValueError):
        limit = 25
    created_by = str(payload.get("created_by") or "")
    aha_leads = [c for c in store.load_instructor_candidates(zip_code)
                 if c.get("source") == "aha_atlas"]
    aha_leads.sort(key=lambda c: (c.get("distance_miles") is None,
                                  c.get("distance_miles") or 9999.0))
    queued, skipped = [], []
    for lead in aha_leads[:limit]:
        lead_id = str(lead.get("id") or "")
        if not lead_id:
            continue
        res = enqueue_lead("INSTRUCTOR", lead_id, zip_code=zip_code,
                           created_by=created_by)
        if res.get("ok"):
            queued.append({"lead_id": lead_id, "name": lead.get("name"),
                           "to_email": res.get("to_email"),
                           "placeholder_blocked": res.get("placeholder_blocked")})
        else:
            skipped.append({"lead_id": lead_id, "name": lead.get("name"),
                            "reason": res.get("error") or res.get("message")})
    return JSONResponse(scrub_sensitive({
        "zip": zip_code,
        "queued": queued,
        "skipped": skipped,
        "queued_count": len(queued),
        "skipped_count": len(skipped),
        "note": (f"{len(queued)} AHA draft(s) queued for approval — review and "
                 "approve to send." if queued
                 else "No AHA leads with an email were available to draft."),
    }))


@router.get("/zip/{zip_code}/site-coverage")
def api_site_coverage(zip_code: str) -> JSONResponse:
    """Existing ALLCPR site coverage + cannibalization + revenue-health of the
    nearest site → 'use existing / fix current / cannibalization risk / open
    test' decision for this ZIP.
    """
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    coverage = existing_site_coverage(zip_code)
    coverage["nearest_revenue_health_site"] = nearest_site_health(zip_code)
    return JSONResponse(scrub_sensitive(coverage))


@router.get("/revenue-health/summary")
def api_revenue_health_summary() -> JSONResponse:
    """Portfolio revenue-health rollup (counts by status, at-risk sites)."""
    return JSONResponse(scrub_sensitive(revenue_health_summary()))


@router.get("/action-queue")
def api_action_queue(zips: str = "", limit: int = 25) -> JSONResponse:
    """Boss-ready daily checklist grouped by task type. ``zips`` is a comma-
    separated list; when omitted, ZIPs that already have stored ops activity.
    """
    requested = [z.strip() for z in zips.split(",") if _valid_zip(z.strip())]
    if not requested:
        active = set()
        for lead in store.load_instructor_candidates():
            if lead.get("zip"):
                active.add(str(lead["zip"]).zfill(5))
        for lead in store.load_space_candidates():
            if lead.get("zip"):
                active.add(str(lead["zip"]).zfill(5))
        requested = sorted(active)
    try:
        lim = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        lim = 25
    return JSONResponse(scrub_sensitive(build_action_queue(requested[:lim])))


@router.get("/zip/{zip_code}/space-leads")
def api_space_leads(zip_code: str) -> JSONResponse:
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    _, spaces = _leads_for_zip(zip_code)
    return JSONResponse(scrub_sensitive({
        "zip": zip_code,
        "count": len(spaces),
        "leads": spaces,
    }))


@router.post("/zip/{zip_code}/refresh")
def api_refresh(zip_code: str, force: bool = False) -> JSONResponse:
    """Re-run instructor/space discovery for one ZIP (cool-down gated)."""
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    if not force and not store.refresh_allowed(zip_code):
        return JSONResponse(
            {
                "error": "refresh_rate_limited",
                "message": ("This ZIP was refreshed recently. Retry later or "
                            "pass ?force=true."),
                "last_refresh_at": store.last_refresh_at(zip_code),
            },
            status_code=429,
        )
    result = _refresh_zip_leads(zip_code)
    return JSONResponse(scrub_sensitive(result))


@router.get("/zip/{zip_code}/search-queries")
def api_search_queries(zip_code: str, city: str = "",
                       state: str = "") -> JSONResponse:
    """Staff-ready Google/LinkedIn searches to source real leads for a ZIP."""
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    return JSONResponse(scrub_sensitive(
        generate_search_queries(zip_code, city=city, state=state)))


@router.get("/instructor-sourcing")
def api_instructor_sourcing(course: str = "AHA_BLS", zip: str = "",
                            city: str = "", state: str = "",
                            free_posts_used: int = 0) -> JSONResponse:
    """Performance-based, eligibility-aware plan to add an instructor for a
    course near an area: target profile + screening bar + activate/bridge/
    source lanes (incl. a ready-to-run Indeed posting plan).
    """
    course = str(course or "").upper()
    if course not in CREDENTIAL_REQUIREMENTS:
        return JSONResponse(
            {"error": "invalid_course",
             "message": f"course must be one of: {', '.join(COURSE_TYPES)}"},
            status_code=400,
        )
    zip_code = _valid_zip(zip) or "" if zip else ""
    # Real local demand + break-even for this ZIP feed the Indeed recommender.
    demand_ctx = economics = None
    if zip_code:
        market = local_market_context(zip_code)
        demand_ctx = market.get("demand")
        economics = site_economics(zip_code,
                                   competitor_ctx=market.get("competitor"),
                                   demand_ctx=demand_ctx)
        if not city.strip():
            city = ((demand_ctx or {}).get("city") or "")
    try:
        free_used = max(0, int(free_posts_used))
    except (TypeError, ValueError):
        free_used = 0
    plan = build_sourcing_plan(course, zip_code=zip_code, city=city.strip(),
                               state=state.strip(),
                               free_posts_used_this_month=free_used,
                               demand_ctx=demand_ctx, economics=economics)
    return JSONResponse(scrub_sensitive(plan))


@router.get("/live-instructor-leads")
def api_live_instructor_leads(zip: str, radius_miles: float = 8.0,
                              limit: int = 12) -> JSONResponse:
    """Live external CPR-business / provider leads near a ZIP (Yelp + Google
    Places + competitor data). User-initiated; key-gated and cached.
    """
    from app.ops.live_sourcing import find_live_instructor_leads  # noqa: PLC0415
    zip_code = _valid_zip(zip)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    try:
        radius = max(1.0, min(25.0, float(radius_miles)))
    except (TypeError, ValueError):
        radius = 8.0
    try:
        lim = max(1, min(30, int(limit)))
    except (TypeError, ValueError):
        lim = 12
    result = find_live_instructor_leads(zip_code, radius_miles=radius,
                                        limit=lim)
    return JSONResponse(scrub_sensitive(result))


@router.get("/site-health-checklist")
def api_site_health_checklist() -> JSONResponse:
    """The weekly ICPIS site-health check (Smart Manikin SOP) as a checklist."""
    from app.ops.recruiting_policy import site_health_checklist  # noqa: PLC0415
    return JSONResponse(scrub_sensitive({"items": site_health_checklist()}))


@router.get("/top-performers")
def api_top_performers(course: str = "", limit: int = 10) -> JSONResponse:
    """Ranked instructor track record (overall, or for a course type)."""
    course_filter = str(course or "").upper() or None
    if course_filter and course_filter not in COURSE_TYPES:
        return JSONResponse(
            {"error": "invalid_course",
             "message": f"course must be one of: {', '.join(COURSE_TYPES)}"},
            status_code=400,
        )
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    rows = load_instructor_performance()
    leaders = top_performers(rows, course=course_filter, limit=limit)
    return JSONResponse(scrub_sensitive({
        "course": course_filter,
        "count": len(leaders),
        "total_instructors": len(rows),
        "performers": leaders,
    }))


@router.post("/outreach/generate")
def api_outreach_generate(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Generate an outreach draft for a lead. Draft only — never sends.

    Body: {lead_type: "instructor"|"space", lead_id, staff_name?, zip?,
           courses?, student_count?}
    """
    lead_type = _LEAD_TYPES.get(str(payload.get("lead_type") or "").lower())
    lead_id = str(payload.get("lead_id") or "")
    if not lead_type or not lead_id:
        return JSONResponse(
            {"error": "invalid_request",
             "message": "lead_type (instructor|space) and lead_id required."},
            status_code=400,
        )
    lead = store.find_lead(lead_type, lead_id)
    if not lead:
        return JSONResponse(
            {"error": "lead_not_found",
             "message": f"No {lead_type.lower()} lead with id {lead_id}."},
            status_code=404,
        )
    staff_name = str(payload.get("staff_name") or "[Staff Name]")
    zip_code = str(payload.get("zip") or lead.get("zip") or "")
    if lead_type == "INSTRUCTOR":
        draft = generate_instructor_outreach(
            lead, zip_code=zip_code, staff_name=staff_name,
            courses=payload.get("courses"))
    else:
        try:
            student_count = int(payload.get("student_count") or 12)
        except (TypeError, ValueError):
            student_count = 12
        draft = generate_space_outreach(
            lead, zip_code=zip_code, staff_name=staff_name,
            student_count=student_count)
    entry = build_outreach_log_entry(lead_type, lead, draft,
                                     created_by=staff_name)
    store.append_outreach_log(entry)
    return JSONResponse(scrub_sensitive({
        "draft": draft,
        "outreach_log_entry": entry,
        "note": "Draft only — review, personalize, and send manually.",
    }))


@router.post("/leads/{lead_type}/{lead_id}/status")
def api_lead_status(lead_type: str, lead_id: str,
                    payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Update CRM status (and optional note/verification) for one lead."""
    target_type = _LEAD_TYPES.get(str(lead_type).lower())
    if not target_type:
        return JSONResponse(
            {"error": "invalid_lead_type",
             "message": "lead_type must be 'instructor' or 'space'."},
            status_code=400,
        )
    status = str(payload.get("status") or "").upper()
    allowed = (INSTRUCTOR_OUTREACH_STATUSES if target_type == "INSTRUCTOR"
               else SPACE_OUTREACH_STATUSES)
    if status not in allowed:
        return JSONResponse(
            {"error": "invalid_status",
             "message": f"status must be one of: {', '.join(allowed)}"},
            status_code=400,
        )
    updates: Dict[str, Any] = {"outreach_status": status}
    note = str(payload.get("note") or "").strip()
    if note:
        existing = store.find_lead(target_type, lead_id) or {}
        prior_notes = str(existing.get("notes") or "").strip()
        updates["notes"] = f"{prior_notes}\n{note}".strip()
    # Credential status may only be upgraded through this explicit CRM call —
    # discovery never sets VERIFIED.
    credential_status = str(payload.get("credential_status") or "").upper()
    if target_type == "INSTRUCTOR" and credential_status in (
            "NEEDS_VERIFICATION", "VERIFIED", "EXPIRED", "REJECTED"):
        updates["credential_status"] = credential_status
        if credential_status == "VERIFIED" and payload.get(
                "verified_certifications"):
            updates["verified_certifications"] = [
                str(c) for c in payload["verified_certifications"]]
    updated = store.update_lead(target_type, lead_id, updates)
    if not updated:
        return JSONResponse(
            {"error": "lead_not_found",
             "message": f"No {target_type.lower()} lead with id {lead_id}."},
            status_code=404,
        )
    return JSONResponse(scrub_sensitive({"lead": updated}))


@router.post("/admin/import-store",
             dependencies=[Depends(require_write_token)])
def api_import_store(payload: Dict[str, Any] = Body(...),
                     mode: str = "merge") -> JSONResponse:
    """Import a full ops-store snapshot pushed from the local machine.

    This is how the real (gitignored) store — imported roster, sourced leads,
    CRM state — reaches a hosted instance whose checkout only ever contains
    tracked files. The tool is served open (no site-wide auth), so this
    endpoint is reachable without a credential; `mode=replace` can overwrite
    the whole store. Put the deployment behind a network allowlist / proxy if
    that matters.
    """
    if mode not in ("merge", "replace"):
        return JSONResponse(
            {"error": "invalid_mode",
             "message": "mode must be 'merge' or 'replace'."},
            status_code=400,
        )
    if not isinstance(payload, dict) or not any(
            isinstance(payload.get(k), (dict, list)) for k in (
                "instructor_candidates", "space_candidates",
                "outreach_log", "refresh_state")):
        return JSONResponse(
            {"error": "invalid_payload",
             "message": "Expected at least one of instructor_candidates, "
                        "space_candidates, outreach_log, refresh_state."},
            status_code=400,
        )
    counts = store.import_store_payload(payload, mode=mode)
    logger.info(f"ops store imported via API (mode={mode}): {counts}")
    return JSONResponse({"ok": True, "mode": mode, "imported": counts})


@router.post("/admin/import-manual",
             dependencies=[Depends(require_write_token)])
def api_import_manual(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Upload the real source CSVs (roster, past-instructor performance,
    locations, revenue health, …) to the hosted instance's persistent disk so
    the matching/coverage/revenue engines have data. Body: {files: {name: csv}}.
    Only whitelisted filenames are accepted; the CSVs are read from MANUAL_DIR
    (set MANUAL_DATA_DIR to a disk path on Render).
    """
    from app.ops.imports import MANUAL_CSV_WHITELIST, save_manual_csv  # noqa: PLC0415
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        return JSONResponse(
            {"error": "invalid_request",
             "message": "Expected {files: {filename: csv_content}} with at "
                        f"least one of: {', '.join(sorted(MANUAL_CSV_WHITELIST))}"},
            status_code=400)
    written, skipped = {}, {}
    for name, content in files.items():
        ok, reason = save_manual_csv(str(name), content)
        if ok:
            written[name] = len(content or "")
        else:
            skipped[name] = reason
    logger.info(f"manual CSVs imported via API: {list(written)} "
                f"(skipped {list(skipped)})")
    return JSONResponse({"ok": bool(written), "written": written,
                         "skipped": skipped})


# --------------------------------------------------------------------------
# Outreach engine (approval-gated sending, follow-ups, reply intake)
# --------------------------------------------------------------------------
@router.get("/outreach/engine-status")
def api_outreach_engine_status() -> JSONResponse:
    return JSONResponse(scrub_sensitive(engine_status()))


@router.get("/outreach/queue")
def api_outreach_queue() -> JSONResponse:
    return JSONResponse(scrub_sensitive(queue_snapshot()))


@router.post("/outreach/queue")
def api_outreach_enqueue(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    target_type = _LEAD_TYPES.get(str(payload.get("lead_type") or "").lower())
    lead_id = str(payload.get("lead_id") or "")
    if not target_type or not lead_id:
        return JSONResponse(
            {"error": "invalid_request",
             "message": "lead_type ('instructor'|'space') and lead_id "
                        "are required."},
            status_code=400,
        )
    result = enqueue_lead(target_type, lead_id,
                          zip_code=str(payload.get("zip") or ""),
                          created_by=str(payload.get("created_by") or ""))
    status = 200 if result.get("ok") else 400
    return JSONResponse(scrub_sensitive(result), status_code=status)


@router.post("/outreach/queue/{lead_id}/edit")
def api_outreach_edit(lead_id: str,
                      payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    result = edit_queued_draft(lead_id,
                               subject=str(payload.get("subject") or ""),
                               body=str(payload.get("body") or ""))
    return JSONResponse(scrub_sensitive(result),
                        status_code=200 if result.get("ok") else 400)


@router.post("/outreach/queue/{lead_id}/cancel")
def api_outreach_cancel(lead_id: str) -> JSONResponse:
    result = cancel_sequence(lead_id)
    return JSONResponse(scrub_sensitive(result),
                        status_code=200 if result.get("ok") else 400)


@router.post("/outreach/approve",
             dependencies=[Depends(require_write_token)])
def api_outreach_approve(payload: Dict[str, Any] = Body(default={})
                         ) -> JSONResponse:
    """Send touch 1 for every pending sequence (or just payload.lead_ids).
    Approving a sequence authorizes its scheduled follow-up touches too."""
    lead_ids = payload.get("lead_ids")
    if lead_ids is not None and not isinstance(lead_ids, list):
        return JSONResponse(
            {"error": "invalid_request", "message": "lead_ids must be a list."},
            status_code=400,
        )
    result = approve_and_send(
        lead_ids=[str(i) for i in lead_ids] if lead_ids is not None else None,
        sent_by=str(payload.get("sent_by") or ""))
    return JSONResponse(scrub_sensitive(result))


@router.post("/outreach/tick", dependencies=[Depends(require_write_token)])
def api_outreach_tick() -> JSONResponse:
    """Engine heartbeat: poll for replies, send due follow-ups. Point a cron
    at this (or click the dashboard button)."""
    return JSONResponse(scrub_sensitive(run_tick()))


@router.get("/write-protection")
def api_write_protection() -> JSONResponse:
    """Whether dangerous writes need the X-Ops-Write-Token header (no secrets)."""
    return JSONResponse(write_protection_status())


# --------------------------------------------------------------------------
# Manatal ATS integration (off by default; never returns the API token).
# Write endpoints respect MANATAL_WRITE_ENABLED (dry-run when off) and the
# optional OPS_WRITE_TOKEN guard.
# --------------------------------------------------------------------------
@router.get("/manatal/status")
def api_manatal_status() -> JSONResponse:
    """Manatal config health (booleans/ids only — never the token)."""
    return JSONResponse(scrub_sensitive(manatal_status()))


@router.get("/manatal/test")
def api_manatal_test() -> JSONResponse:
    """Read-only probe: does the token actually authenticate with Manatal?
    Lists one candidate and returns only ok/status/count — never candidate data.
    """
    from app.ops.manatal_client import ManatalClient  # noqa: PLC0415
    result = ManatalClient().test_connection()
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    ok = bool(result.get("ok"))
    out = {
        "ok": ok,
        "mode": manatal_status()["mode"],
        "status_code": result.get("status_code"),
        "candidate_count": data.get("count"),
        "message": ("Connected — token authenticates with Manatal." if ok
                    else result.get("message") or result.get("error")
                    or "Not connected."),
    }
    return JSONResponse(scrub_sensitive(out))


@router.get("/manatal/directory")
def api_manatal_directory() -> JSONResponse:
    """Read-only id discovery for connector setup: client organizations and
    account users, as id+name pairs ONLY (for MANATAL_DEFAULT_ORGANIZATION /
    MANATAL_DEFAULT_OWNER_ID). Never emails, tokens, or full records.
    """
    from app.ops.manatal_client import ManatalClient  # noqa: PLC0415
    client = ManatalClient()

    def _pairs(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = (row.get("name")
                    or " ".join(p for p in (row.get("first_name"),
                                            row.get("last_name")) if p)
                    or row.get("username") or "")
            out.append({"id": row.get("id"), "name": str(name).strip()})
        return out

    orgs = client.list_organizations(limit=50)
    users = client.list_users(limit=50)

    # Sample existing jobs so setup can copy the org/owner the team already
    # uses (ids only — no descriptions, salaries, or candidate data).
    jobs = client._request("GET", f"{client._jobs_path()}?limit=5",
                           write=False, action="sample_jobs")
    job_rows = []
    jdata = jobs.get("data") if isinstance(jobs.get("data"), dict) else {}
    for row in (jdata.get("results") or []):
        if isinstance(row, dict):
            job_rows.append({"id": row.get("id"),
                             "position_name": row.get("position_name"),
                             "organization": row.get("organization"),
                             "owner": row.get("owner")})

    return JSONResponse(scrub_sensitive({
        "ok": bool(orgs.get("ok") or users.get("ok")),
        "mode": manatal_status()["mode"],
        "organizations": _pairs(orgs),
        "users": _pairs(users),
        "sample_jobs": job_rows,
        "organizations_error": None if orgs.get("ok") else (
            orgs.get("message") or orgs.get("error")),
        "users_error": None if users.get("ok") else (
            users.get("message") or users.get("error")),
    }))


def _manatal_zip_ctx(zip_code: str, course: str) -> Dict[str, Any]:
    """Best-effort demand/site context for a Manatal job (all optional)."""
    ctx: Dict[str, Any] = {}
    try:
        market = local_market_context(zip_code)
        demand = market.get("demand") or {}
        ctx["city"] = demand.get("city") or ""
        ctx["state"] = demand.get("state") or ""
        ctx["demand_score"] = demand.get("demand_score")
    except Exception:  # noqa: BLE0001 — context is optional, never fatal
        pass
    return ctx


@router.post("/manatal/jobs/create",
             dependencies=[Depends(require_write_token)])
def api_manatal_create_job(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    zip_code = _valid_zip(str(payload.get("zip") or ""))
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    course = str(payload.get("course") or "OVERALL").upper()
    ctx = _manatal_zip_ctx(zip_code, course)
    ctx.update({k: payload[k] for k in ("city", "state", "why", "nearest_site",
                                        "demand_score", "readiness_score")
                if payload.get(k) is not None})
    return JSONResponse(scrub_sensitive(
        manatal_sync.create_job_for_zip(zip_code, course, ctx=ctx)))


@router.post("/manatal/candidates/push",
             dependencies=[Depends(require_write_token)])
def api_manatal_push_candidate(payload: Dict[str, Any] = Body(...)
                               ) -> JSONResponse:
    lead_id = str(payload.get("lead_id") or "")
    if not lead_id:
        return JSONResponse(
            {"error": "invalid_request", "message": "lead_id is required."},
            status_code=400)
    zip_code = _valid_zip(str(payload.get("zip") or "")) or ""
    course = str(payload.get("course") or "")
    ctx = _manatal_zip_ctx(zip_code, course) if zip_code else {}
    return JSONResponse(scrub_sensitive(manatal_sync.push_lead(
        lead_id, zip_code=zip_code, course=course, ctx=ctx)))


@router.post("/manatal/zip/{zip_code}/push-top-leads",
             dependencies=[Depends(require_write_token)])
def api_manatal_push_top_leads(zip_code: str,
                               payload: Dict[str, Any] = Body(default={})
                               ) -> JSONResponse:
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    try:
        limit = max(1, min(20, int(payload.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    return JSONResponse(scrub_sensitive(manatal_sync.push_top_leads(
        zip_code, limit=limit, course=str(payload.get("course") or ""))))


@router.get("/manatal/zip/{zip_code}/sync-status")
def api_manatal_sync_status(zip_code: str) -> JSONResponse:
    zip_code = _valid_zip(zip_code)
    if not zip_code:
        return JSONResponse(_INVALID_ZIP, status_code=400)
    return JSONResponse(scrub_sensitive(manatal_sync.sync_zip(zip_code)))


@router.post("/manatal/candidates/{candidate_id}/sync",
             dependencies=[Depends(require_write_token)])
def api_manatal_sync_candidate(candidate_id: str) -> JSONResponse:
    return JSONResponse(scrub_sensitive(
        manatal_sync.sync_candidate(str(candidate_id))))


@router.delete("/manatal/candidates/{candidate_id}",
               dependencies=[Depends(require_write_token)])
def api_manatal_delete_candidate(candidate_id: str) -> JSONResponse:
    return JSONResponse(scrub_sensitive(
        manatal_sync.delete_candidate(str(candidate_id))))
