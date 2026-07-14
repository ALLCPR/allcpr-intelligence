"""
Action Queue — the boss/helper daily checklist.

Turns the whole engine into "what should staff do next", grouped:
    1. Instructor Recruiting Needed
    2. Space Outreach Needed
    3. Manatal Sync Needed
    4. Revenue Leakage / Critical Site Review
    5. Test Class Ready
    6. Existing Site Coverage Conflict
    7. Missing Manual Data

Each task carries a ZIP/site, priority, reason, recommended owner, next action,
optional due date, and a dashboard link. The per-ZIP engine calls are injectable
so this is testable without CSVs or ZIP centroids.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.ops import store
from app.ops.coverage import existing_site_coverage
from app.ops.imports import (
    COURSE_ECONOMICS_FILE,
    INSTRUCTORS_FILE,
    LOCATIONS_FILE,
)
from app.ops.instructor_matching import match_instructors_for_zip
from app.ops.manatal_client import status as manatal_status
from app.ops.revenue_health import REVENUE_HEALTH_FILE, revenue_health_summary

GROUPS = (
    "Instructor Recruiting Needed",
    "Space Outreach Needed",
    "Manatal Sync Needed",
    "Revenue Leakage / Critical Site Review",
    "Test Class Ready",
    "Existing Site Coverage Conflict",
    "Missing Manual Data",
)

# Space CRM statuses that mean a room path is already in hand.
_SPACE_READY = frozenset({"AVAILABLE", "GOOD_FIT", "CONFIRMED"})
# Instructor levels that mean a teacher path is in hand (contacted+).
_INSTRUCTOR_IN_HAND = 4


def _task(zip_or_site: str, priority: str, reason: str, next_action: str,
          owner: str = "", due_date: str = "", link: str = "") -> Dict[str, Any]:
    return {
        "ref": zip_or_site,
        "priority": priority,
        "reason": reason,
        "next_action": next_action,
        "owner": owner or "Unassigned",
        "due_date": due_date or None,
        "link": link or (f"/?zip={zip_or_site}" if zip_or_site.isdigit() else ""),
    }


def _missing_manual_data() -> List[Dict[str, Any]]:
    checks = [
        (INSTRUCTORS_FILE, "Instructor roster",
         "Add data/manual/allcpr_instructors.csv (past + active instructors)."),
        (LOCATIONS_FILE, "ALLCPR locations",
         "Add data/manual/allcpr_locations.csv (active/inactive sites)."),
        (REVENUE_HEALTH_FILE, "Revenue health",
         "Add data/manual/site_revenue_health.csv (site profitability)."),
        (COURSE_ECONOMICS_FILE, "Course economics",
         "Add data/manual/course_economics.csv (price/cost/break-even)."),
    ]
    out = []
    for path, label, action in checks:
        if not Path(path).exists():
            out.append(_task(label, "P2", f"{label} data not loaded",
                             action, owner="Data / Ops"))
    return out


def build_action_queue(
    zips: Optional[List[str]] = None,
    *,
    match_fn: Callable[[str], Dict[str, Any]] = match_instructors_for_zip,
    coverage_fn: Callable[[str], Dict[str, Any]] = existing_site_coverage,
    space_leads_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    revenue_summary_fn: Callable[[], Dict[str, Any]] = revenue_health_summary,
    missing_data_fn: Callable[[], List[Dict[str, Any]]] = _missing_manual_data,
    manatal_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Grouped task list for a set of ZIPs (+ portfolio-level tasks)."""
    zips = [str(z).zfill(5) for z in (zips or [])]
    space_leads_fn = space_leads_fn or (
        lambda z: store.load_space_candidates(z))
    manatal_mode = manatal_mode or manatal_status()["mode"]
    groups: Dict[str, List[Dict[str, Any]]] = {g: [] for g in GROUPS}

    for zip_code in zips:
        match = match_fn(zip_code)
        coverage = coverage_fn(zip_code)
        spaces = space_leads_fn(zip_code) or []
        best = (match.get("best_instructor_path") or [])
        top_level = max((c.get("lead_level") or 0 for c in best), default=0)
        instructor_ready = top_level >= _INSTRUCTOR_IN_HAND
        space_ready = any(str(s.get("outreach_status") or "").upper()
                          in _SPACE_READY for s in spaces)
        cov_decision = coverage.get("coverage_decision")

        # 1. Instructor recruiting
        if not instructor_ready and match.get("recommended_action") in (
                "CONTACT_PAST_INSTRUCTOR", "CONTACT_NAMED_LEAD",
                "SOURCE_INSTRUCTORS", "NO_INSTRUCTOR_PATH"):
            groups["Instructor Recruiting Needed"].append(_task(
                zip_code, "P1", match.get("recommended_action_label", ""),
                match.get("explanation", "Source/contact instructors."),
                owner="Recruiting"))

        # 2. Space outreach
        if not space_ready:
            n = len([s for s in spaces if s.get("name")])
            groups["Space Outreach Needed"].append(_task(
                zip_code, "P1", "No room secured yet",
                (f"Contact {n} room candidate(s)" if n
                 else "Source room candidates (generate room search queries)"),
                owner="Space / BD"))

        # 3. Manatal sync
        linked = [l for l in store.load_instructor_candidates(zip_code)
                  if l.get("manatal_candidate_id")]
        if linked and manatal_mode != "DISABLED":
            groups["Manatal Sync Needed"].append(_task(
                zip_code, "P2", f"{len(linked)} candidate(s) linked to Manatal",
                "Pull latest Manatal stage → update readiness", owner="Recruiting"))

        # 5. Test class ready
        if (instructor_ready and space_ready
                and cov_decision != "FIX_CURRENT_FIRST"):
            groups["Test Class Ready"].append(_task(
                zip_code, "P0", "Instructor + room + demand aligned",
                "Schedule a test class", owner="Operations"))

        # 6. Coverage conflict
        if cov_decision in ("USE_EXISTING_SITE", "FIX_CURRENT_FIRST",
                            "CANNIBALIZATION_RISK"):
            groups["Existing Site Coverage Conflict"].append(_task(
                zip_code,
                "P0" if cov_decision == "FIX_CURRENT_FIRST" else "P1",
                coverage.get("coverage_decision_label", ""),
                coverage.get("explanation", ""), owner="Strategy / Ops"))

    # 4. Revenue leakage / critical (portfolio-level)
    summary = revenue_summary_fn()
    for site in summary.get("at_risk_sites", []):
        groups["Revenue Leakage / Critical Site Review"].append(_task(
            site, "P0", "Site flagged critical/leakage",
            "Review revenue health; fix / relocate / merge",
            owner="Finance / Ops"))

    # 7. Missing manual data (portfolio-level)
    groups["Missing Manual Data"].extend(missing_data_fn())

    counts = {g: len(t) for g, t in groups.items()}
    return {
        "generated_for_zips": zips,
        "groups": groups,
        "counts": counts,
        "total_tasks": sum(counts.values()),
        "manatal_mode": manatal_mode,
    }
