"""
Enrollware site coverage + cannibalization logic.

Uses the imported ALLCPR location list (active/inactive) + the revenue-health
layer to answer, for a target ZIP: does an existing site already cover this,
would opening here cannibalize a strong nearby site, or should we fix a weak
nearby site first — before we ever recommend a new shared-space test.

Decisions (fed into the readiness recommendation + action queue):
    USE_EXISTING_SITE     a healthy site already covers this ZIP → add classes
    FIX_CURRENT_FIRST     nearest site is critical/leaking → fix/relocate first
    CANNIBALIZATION_RISK  a strong site is close → new site would steal from it
    OPEN_TEST_OK          no nearby active coverage → new test on the table
                          (if instructor + room are not blocked)
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from app.ops.imports import load_locations_import
from app.ops.instructor_supply import distance_between_zips
from app.ops.revenue_health import load_revenue_health

# Distance bands (miles).
COVERAGE_RADIUS = 6.0      # within this, an existing site "covers" the ZIP
NEARBY_RADIUS = 12.0       # within this, a strong site can be cannibalized
MAX_CONSIDER = 15.0        # beyond this, treat as no nearby coverage

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(name: Any) -> str:
    return _NON_ALNUM.sub(" ", str(name or "").lower()).strip()


def _match_health(location: Dict[str, Any],
                  health_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the revenue-health row for a location (by ZIP, then by name)."""
    lz = str(location.get("zip") or "")
    ln = _norm(location.get("location_name"))
    for r in health_rows:
        if lz and r.get("zip") == lz:
            return r
    for r in health_rows:
        rn = _norm(r.get("site_name"))
        if rn and (rn in ln or ln in rn):
            return r
    return None


def _site_view(location: Dict[str, Any], dist: Optional[float],
               health: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "name": location.get("location_name"),
        "zip": location.get("zip"),
        "distance_miles": dist,
        "courses_offered": location.get("courses_offered") or [],
        "average_monthly_enrollment": location.get("average_monthly_enrollment"),
        "health_status": (health or {}).get("health_status"),
        "health_label": (health or {}).get("health_label"),
        "monthly_avg_revenue": (health or {}).get("monthly_avg_revenue"),
        "break_even_gap": (health or {}).get("break_even_gap"),
        "trend": (health or {}).get("trend"),
        "responsible_team": (health or {}).get("responsible_team"),
        "action_due_date": (health or {}).get("action_due_date"),
        "flags": (health or {}).get("flags") or [],
    }


def existing_site_coverage(
    zip_code: str,
    *,
    locations: Optional[List[Dict[str, Any]]] = None,
    health_rows: Optional[List[Dict[str, Any]]] = None,
    distance_fn: Optional[Callable[[str, str], Optional[float]]] = None,
) -> Dict[str, Any]:
    """Existing-coverage + cannibalization assessment for a ZIP."""
    zip_code = str(zip_code).zfill(5)
    locations = load_locations_import() if locations is None else locations
    health_rows = load_revenue_health() if health_rows is None else health_rows
    distance_fn = distance_fn or distance_between_zips

    def _with_dist(loc):
        d = distance_fn(str(loc.get("zip") or ""), zip_code)
        return loc, (d if d is not None else 9999.0)

    active = [_with_dist(l) for l in locations
              if str(l.get("active_status") or "").lower() == "active"
              and l.get("zip")]
    inactive = [_with_dist(l) for l in locations
                if str(l.get("active_status") or "").lower() != "active"
                and l.get("zip")]
    active.sort(key=lambda t: t[1])
    inactive.sort(key=lambda t: t[1])

    nearest_active = nearest_active_dist = nearest_active_health = None
    if active:
        loc, d = active[0]
        nearest_active = loc
        nearest_active_dist = None if d >= 9999.0 else round(d, 1)
        nearest_active_health = _match_health(loc, health_rows)

    nearest_inactive = None
    if inactive:
        loc, d = inactive[0]
        dist = None if d >= 9999.0 else round(d, 1)
        nearest_inactive = _site_view(loc, dist,
                                      _match_health(loc, health_rows))

    covered = (nearest_active_dist is not None
               and nearest_active_dist <= COVERAGE_RADIUS)
    is_weak = bool((nearest_active_health or {}).get("is_weak"))
    is_strong = bool((nearest_active_health or {}).get("is_strong"))
    within_nearby = (nearest_active_dist is not None
                     and nearest_active_dist <= NEARBY_RADIUS)

    decision, label, why = _decide(nearest_active_dist, covered, is_weak,
                                   is_strong, within_nearby)
    cannibalization = decision == "CANNIBALIZATION_RISK" or (
        covered and is_strong)

    warnings: List[str] = []
    if nearest_active_health:
        warnings.extend(nearest_active_health.get("flags") or [])
    if cannibalization:
        warnings.append("Opening here may cannibalize a strong nearby site.")

    return {
        "zip": zip_code,
        "covered_by_existing": covered,
        "cannibalization_risk": cannibalization,
        "distance_to_nearest_active": nearest_active_dist,
        "nearest_active_site": (
            _site_view(nearest_active, nearest_active_dist,
                       nearest_active_health) if nearest_active else None),
        "nearest_inactive_site": nearest_inactive,
        "coverage_decision": decision,
        "coverage_decision_label": label,
        "explanation": why,
        "warnings": warnings,
    }


def _decide(dist: Optional[float], covered: bool, is_weak: bool,
            is_strong: bool, within_nearby: bool):
    if dist is None or dist > MAX_CONSIDER:
        return ("OPEN_TEST_OK", "New Test On The Table",
                "No active ALLCPR site nearby — a new shared-space test is on "
                "the table if instructor and room are not blocked.")
    if covered:
        if is_weak:
            return ("FIX_CURRENT_FIRST", "Fix Current Site First",
                    "A nearby site already covers this ZIP but is critical/"
                    "leaking — fix or relocate it before expanding here.")
        return ("USE_EXISTING_SITE", "Use Existing Site",
                "A healthy existing site already covers this ZIP — add classes "
                "there instead of opening a new location.")
    if within_nearby and is_strong:
        return ("CANNIBALIZATION_RISK", "Cannibalization Risk",
                "A strong site is close by — opening here may cannibalize it; "
                "prefer adding classes at the existing site.")
    if within_nearby and is_weak:
        return ("FIX_CURRENT_FIRST", "Fix Current Site First",
                "A weak/critical site is close by — stabilize or relocate it "
                "before opening another nearby location.")
    return ("OPEN_TEST_OK", "New Test On The Table",
            "No existing site covers this ZIP — a new shared-space test is on "
            "the table if instructor and room are not blocked.")
