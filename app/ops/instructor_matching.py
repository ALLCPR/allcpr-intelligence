"""
Instructor-to-ZIP matching (the "best instructor path" for a target ZIP).

This is the operational heart of instructor supply: for a given ZIP it answers
"who should we contact to teach here, in what order?" — and it encodes the key
business rule that a **past ALLCPR/Enrollware instructor** (someone who has
actually taught classes) is a far stronger lead than a random professor.

Sources merged, strongest first:
    Level 6  confirmed long-term instructor          (CRM confirmed)
    Level 5  credential-verified instructor          (staff verified / ATS)
    Level 4  contacted / interested candidate         (in conversation)
    Level 3  PAST ALLCPR / Enrollware instructor      (proven track record)
    Level 2  named professor / instructor lead        (named, no track record)
    Level 1  institutional signal                     (a nursing school nearby)

Past instructors come from the performance master (the 6-month student export,
``instructor_performance.py``): each carries a home ZIP, so we can compute the
distance to the target ZIP and rank by "closest proven instructor first". They
are merged by name with the ZIP's stored CRM leads so recruiting progress
(contacted / verified / Manatal stage) is reflected in the level.

Everything is offline + deterministic (roster + performance CSV + ZIP
centroids); no network. Ranking never promotes a lead past its honest level.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from app.ops import store
from app.ops.instructor_performance import load_instructor_performance
from app.ops.instructor_supply import (
    DEFAULT_TRAVEL_RADIUS_MILES,
    distance_between_zips,
)
from app.ops.models import (
    LEAD_LEVEL_LABELS,
    LEVEL_CONFIRMED,
    LEVEL_CONTACTED,
    LEVEL_CREDENTIAL_VERIFIED,
    LEVEL_INSTITUTIONAL_SIGNAL,
    LEVEL_NAMED_LEAD,
    LEVEL_PAST_INSTRUCTOR,
    lead_level,
)

# How far a proven instructor's home ZIP can be from the target before we stop
# treating them as a realistic match (generous — proven instructors travel).
MATCH_RADIUS_MILES = 25.0

# Base match score per level. Mirrors the Manatal stage→readiness ladder
# (contacted ~50-65, verified 85, ready 100); a proven-but-cold past instructor
# (58) outranks any un-taught named professor lead (30).
_LEVEL_BASE = {
    LEVEL_INSTITUTIONAL_SIGNAL: 12,
    LEVEL_NAMED_LEAD: 30,
    LEVEL_PAST_INSTRUCTOR: 58,
    LEVEL_CONTACTED: 65,
    LEVEL_CREDENTIAL_VERIFIED: 85,
    LEVEL_CONFIRMED: 100,
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: Any) -> str:
    """Lowercase, punctuation-stripped key for deduping people by name."""
    return _NON_ALNUM.sub(" ", str(name or "").lower()).strip()


def _past_instructor_track(row: Dict[str, Any]) -> Dict[str, Any]:
    """The proven-track-record fields carried onto a matched lead."""
    return {
        "past_instructor": True,
        "performance_score": row.get("performance_score"),
        "performance_tier": row.get("performance_tier"),
        "proven_courses": list(row.get("proven_courses") or []),
        "past_students": row.get("students"),
        "past_classes": row.get("classes"),
        "last_taught": row.get("last_taught"),
        "company_grade": row.get("company_grade"),
    }


def _match_score(lead: Dict[str, Any], course: Optional[str]) -> float:
    """0..100 within-level score: distance + course fit + track record + reach.

    Level dominates the ranking (see _sort_key); this only orders leads that
    share a level and drives the displayed number.
    """
    level = lead_level(lead)
    score = float(_LEVEL_BASE.get(level, 12))

    dist = lead.get("distance_miles")
    if dist is not None:
        if dist <= 10:
            score += 8
        elif dist <= DEFAULT_TRAVEL_RADIUS_MILES:
            score += 3
        else:
            score -= 12

    proven = lead.get("proven_courses") or []
    possible = lead.get("courses_possible") or []
    if course:
        if course in proven:
            score += 8
        elif course in possible:
            score += 4
        else:
            score -= 6

    tier = lead.get("performance_tier")
    if tier == "Top Performer":
        score += 6
    elif tier == "Solid":
        score += 3

    if lead.get("email") or lead.get("phone"):
        score += 4

    return round(max(0.0, min(100.0, score)), 1)


def _why(lead: Dict[str, Any], course: Optional[str]) -> List[str]:
    reasons: List[str] = [LEAD_LEVEL_LABELS[lead_level(lead)]]
    if lead.get("past_instructor"):
        taught = lead.get("past_students")
        reasons.append(
            f"Taught {taught} students before"
            if taught else "Has taught ALLCPR classes before")
        if course and course in (lead.get("proven_courses") or []):
            reasons.append(f"Proven in this course ({course})")
    dist = lead.get("distance_miles")
    if dist is not None:
        reasons.append(f"{dist} mi from ZIP")
    if lead.get("email") or lead.get("phone"):
        reasons.append("Contactable")
    return reasons


def _sort_key(lead: Dict[str, Any]):
    # Level first (honest strength), then match score, then nearest.
    dist = lead.get("distance_miles")
    dist_key = dist if dist is not None else 9999.0
    return (-lead_level(lead), -(lead.get("match_score") or 0.0), dist_key)


def match_instructors_for_zip(
    zip_code: str,
    *,
    course: Optional[str] = None,
    limit: int = 8,
    radius_miles: float = MATCH_RADIUS_MILES,
    stored_leads: Optional[List[Dict[str, Any]]] = None,
    performance_rows: Optional[List[Dict[str, Any]]] = None,
    distance_fn: Optional[Callable[[str, str], Optional[float]]] = None,
) -> Dict[str, Any]:
    """Ranked instructor path for a ZIP: past instructors > leads > signals.

    Args mostly exist for testing; in production the ZIP's stored CRM leads and
    the performance master are loaded automatically.
    """
    zip_code = str(zip_code).zfill(5)
    course = (str(course).upper() or None) if course else None
    distance_fn = distance_fn or distance_between_zips
    if stored_leads is None:
        stored_leads = store.load_instructor_candidates(zip_code)
    if performance_rows is None:
        performance_rows = load_instructor_performance()

    pool: List[Dict[str, Any]] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    for lead in stored_leads:
        cand = dict(lead)
        if cand.get("distance_miles") is None and cand.get("zip"):
            cand["distance_miles"] = distance_fn(str(cand["zip"]), zip_code)
        pool.append(cand)
        key = normalize_name(cand.get("name"))
        if key and not cand.get("source") == "zip_enrichment_signal":
            by_name[key] = cand

    # Merge in proven past instructors from the performance master. Ones
    # beyond the radius go to far_pool — used only as a fallback when nothing
    # is genuinely nearby, so "any ZIP" still gets an honest answer.
    far_pool: List[Dict[str, Any]] = []
    for row in performance_rows:
        key = normalize_name(row.get("name"))
        if not key:
            continue
        dist = distance_fn(str(row.get("home_zip") or ""), zip_code)
        track = _past_instructor_track(row)
        if key in by_name:
            existing = by_name[key]
            for fld, val in track.items():
                if val not in (None, [], ""):
                    existing[fld] = val
            if existing.get("distance_miles") is None:
                existing["distance_miles"] = dist
            continue
        if dist is None and str(row.get("home_zip") or "").zfill(5) != zip_code:
            continue  # unknown home location — can't honestly call it "nearby"
        cand = {
            "id": None,
            "name": row.get("name"),
            "raw_name": row.get("name"),
            "source": "enrollware_performance",
            "organization": "ALLCPR (past instructor)",
            "city": row.get("home_city"),
            "state": row.get("home_state"),
            "zip": row.get("home_zip"),
            "distance_miles": dist,
            # Proven teaching ≠ auto "VERIFIED": re-confirm current cert +
            # availability. Level 3 (past instructor) until staff verify.
            "credential_status": "NEEDS_VERIFICATION",
            "outreach_status": "NEW",
            "courses_possible": list(row.get("proven_courses") or []),
            **track,
        }
        if dist is not None and dist > radius_miles:
            cand["beyond_radius"] = True
            far_pool.append(cand)
            continue
        pool.append(cand)
        by_name[key] = cand

    # Fallback: nothing real (named/past) within radius → surface the nearest
    # few past instructors anyway, honestly labeled with their true distance.
    expanded_search = False
    has_real_nearby = any(
        c.get("past_instructor")
        or (str(c.get("name") or "").strip()
            and c.get("source") != "zip_enrichment_signal")
        for c in pool)
    if not has_real_nearby and far_pool:
        far_pool.sort(key=lambda c: c.get("distance_miles") or 9999.0)
        for cand in far_pool[:3]:
            pool.append(cand)
            by_name[normalize_name(cand.get("name"))] = cand
        expanded_search = True

    # Optional course filter (keep signals — they seed sourcing for any course).
    if course:
        pool = [c for c in pool
                if course in (c.get("proven_courses") or [])
                or course in (c.get("courses_possible") or [])
                or c.get("source") == "zip_enrichment_signal"]

    ranked: List[Dict[str, Any]] = []
    for cand in pool:
        level = lead_level(cand)
        cand["lead_level"] = level
        cand["lead_level_label"] = LEAD_LEVEL_LABELS[level]
        cand["normalized_name"] = normalize_name(cand.get("name"))
        cand["match_score"] = _match_score(cand, course)
        cand["match_reasons"] = _why(cand, course)
        ranked.append(cand)
    ranked.sort(key=_sort_key)

    counts_by_level = {LEAD_LEVEL_LABELS[lvl]: 0 for lvl in LEAD_LEVEL_LABELS}
    for cand in ranked:
        counts_by_level[cand["lead_level_label"]] += 1

    action, label, explanation = _recommend(ranked, radius_miles)
    return {
        "zip": zip_code,
        "course": course,
        "count": len(ranked),
        "best_instructor_path": ranked[:max(1, limit)],
        "counts_by_level": counts_by_level,
        "recommended_action": action,
        "recommended_action_label": label,
        "explanation": explanation,
        "search_radius_miles": radius_miles,
        "expanded_search": expanded_search,
    }


def _recommend(ranked: List[Dict[str, Any]], radius: float):
    """Top of the ladder present decides the next action."""
    def _in_radius(c):
        d = c.get("distance_miles")
        return d is None or d <= radius

    levels = {c["lead_level"] for c in ranked if _in_radius(c)}
    if LEVEL_CONFIRMED in levels:
        return ("INSTRUCTOR_CONFIRMED", "Instructor Confirmed",
                "A confirmed instructor is ready to teach here.")
    if LEVEL_CREDENTIAL_VERIFIED in levels:
        return ("CONFIRM_AVAILABILITY", "Confirm Availability",
                "A credential-verified instructor is available nearby — "
                "confirm availability and rate.")
    if LEVEL_CONTACTED in levels:
        return ("ADVANCE_IN_MANATAL", "Advance In Recruiting",
                "A candidate is already in conversation — advance them in "
                "Manatal (credential check / rate confirm).")
    past = [c for c in ranked
            if c["lead_level"] == LEVEL_PAST_INSTRUCTOR and _in_radius(c)]
    if past:
        nearest = past[0]
        dist = nearest.get("distance_miles")
        where = f" (~{dist} mi)" if dist is not None else ""
        return ("CONTACT_PAST_INSTRUCTOR", "Contact Past Instructor First",
                f"Fastest path: contact {nearest.get('name')}{where} — a proven "
                "ALLCPR instructor — before sourcing anyone new.")
    named = [c for c in ranked
             if c["lead_level"] == LEVEL_NAMED_LEAD and _in_radius(c)]
    if named:
        return ("CONTACT_NAMED_LEAD", "Contact Named Lead",
                "No past instructor nearby — contact the named professor/"
                "instructor lead(s) and source more.")
    far = [c for c in ranked if c.get("beyond_radius")]
    if far:
        nearest = min(far, key=lambda c: c.get("distance_miles") or 9999.0)
        return ("CONTACT_NEAREST_FAR", "Nearest Instructor Is Far",
                f"No instructor within {int(radius)} mi of this ZIP. Nearest "
                f"past ALLCPR instructor: {nearest.get('name')} "
                f"(~{nearest.get('distance_miles')} mi, "
                f"{nearest.get('city') or '?'} {nearest.get('state') or ''})"
                " — confirm willingness to travel, or source locally.")
    if any(c["lead_level"] == LEVEL_INSTITUTIONAL_SIGNAL for c in ranked):
        return ("SOURCE_INSTRUCTORS", "Source Instructors",
                "Only institutional signals so far — run live search / Indeed "
                "to find named instructors.")
    return ("NO_INSTRUCTOR_PATH", "No Instructor Path",
            "No instructor path found nearby — source externally.")
