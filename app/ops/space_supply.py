"""
Shared-space classroom discovery + CPR/BLS fit scoring.

Sources today (offline): the imported ALLCPR location list, the manual
commercial-validation CSV, and institutional signals from the enriched ZIP
record (community facilities, colleges, hotels are future live sources).
Live marketplace listings (Peerspace/LiquidSpace-style) can be layered in
later; anything unverified stays a lead with unknown fit facts.

Hard-elimination rules mirror the staff SOP's operational logic (no access
control possible, no camera install allowed, unusable Wi-Fi, unsafe entry,
uncooperative landlord, training use not allowed, room unsuitable) without
reproducing any private access details.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ops.imports import (
    load_locations_import,
    load_room_budget_rules,
)
from app.ops.models import (
    STAGE_BLOCKED,
    STAGE_CANDIDATE_FOUND,
    STAGE_CONFIRMED,
    STAGE_NO_SIGNAL,
    STAGE_SIGNAL_ONLY,
    SharedSpaceCandidate,
    band_label,
)

# Hard-elimination / high-risk flags (SOP operational logic).
FLAG_TRAINING_NOT_ALLOWED = "training_use_not_allowed"
FLAG_NO_ACCESS_CONTROL = "access_control_impossible"
FLAG_NO_CAMERA = "camera_install_not_allowed"
FLAG_BAD_WIFI = "wifi_cannot_support_equipment"
FLAG_UNSAFE_ENTRY = "unsafe_evening_weekend_entry"
FLAG_UNCOOPERATIVE = "landlord_uncooperative"
FLAG_ROOM_UNSUITABLE = "room_size_or_flow_unsuitable"
FLAG_NO_RECURRING = "recurring_use_not_allowed"

HARD_ELIMINATION_FLAGS = (
    FLAG_TRAINING_NOT_ALLOWED,
    FLAG_NO_ACCESS_CONTROL,
    FLAG_NO_CAMERA,
    FLAG_BAD_WIFI,
    FLAG_UNSAFE_ENTRY,
    FLAG_UNCOOPERATIVE,
    FLAG_ROOM_UNSUITABLE,
    FLAG_NO_RECURRING,
)

# Note keywords that indicate a landlord/safety problem staff recorded.
_UNSAFE_KEYWORDS = ("unsafe", "no lighting", "sketchy at night")
_UNCOOPERATIVE_KEYWORDS = ("uncooperative", "landlord refused", "hostile")
_UNSUITABLE_KEYWORDS = ("unsuitable", "no open floor", "fixed seating",
                        "pillars block", "too small")


def hard_elimination_flags(space: Dict[str, Any]) -> List[str]:
    """SOP hard-elimination rules. Only explicit ``False`` facts eliminate —
    ``None`` (unchecked) is a verification gap, not a rejection."""
    flags: List[str] = []
    if space.get("training_use_allowed") is False:
        flags.append(FLAG_TRAINING_NOT_ALLOWED)
    if space.get("access_control_possible") is False:
        flags.append(FLAG_NO_ACCESS_CONTROL)
    if space.get("camera_allowed") is False:
        flags.append(FLAG_NO_CAMERA)
    if space.get("wifi") is False:
        flags.append(FLAG_BAD_WIFI)
    if space.get("recurring_available") is False:
        flags.append(FLAG_NO_RECURRING)
    notes = " ".join((
        str(space.get("notes") or ""),
        str(space.get("floor_space_notes") or ""),
        str(space.get("parking_notes") or ""),
    )).lower()
    if any(k in notes for k in _UNSAFE_KEYWORDS):
        flags.append(FLAG_UNSAFE_ENTRY)
    if any(k in notes for k in _UNCOOPERATIVE_KEYWORDS):
        flags.append(FLAG_UNCOOPERATIVE)
    if any(k in notes for k in _UNSUITABLE_KEYWORDS):
        flags.append(FLAG_ROOM_UNSUITABLE)
    return flags


def score_space_candidate(space: Dict[str, Any],
                          room_budget: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
    """Attach ``classroom_fit_score`` (0..100), ``fit_reasons``,
    ``hard_elimination_flags`` and ``confidence_score`` to a space.

    Positive facts add points; unknown facts add nothing (they become
    verification questions); any hard-elimination flag zeroes the fit.
    """
    if room_budget is None:
        room_budget = load_room_budget_rules()
    flags = hard_elimination_flags(space)
    reasons: List[str] = []
    score = 0.0
    checked = 0
    total_checks = 10

    def _fact(value: Optional[bool], points: float, yes: str, no: str) -> float:
        nonlocal checked
        if value is None:
            return 0.0
        checked += 1
        if value:
            reasons.append(yes)
            return points
        reasons.append(no)
        return 0.0

    capacity = space.get("capacity")
    min_capacity = room_budget.get("minimum_capacity") or 8
    if capacity is not None:
        checked += 1
        if capacity >= max(min_capacity, 6):
            score += 15
            reasons.append(f"Capacity {int(capacity)} meets the "
                           f"{int(min_capacity)}-student minimum")
        else:
            reasons.append(f"Capacity {int(capacity)} below the "
                           f"{int(min_capacity)}-student minimum")

    floor_notes = str(space.get("floor_space_notes") or "").lower()
    if floor_notes:
        checked += 1
        if any(k in floor_notes for k in ("open floor", "manikin", "movable",
                                          "flexible", "clear space")):
            score += 12
            reasons.append("Open floor space suitable for manikins")

    score += _fact(space.get("movable_tables_chairs"), 8,
                   "Movable tables/chairs", "Fixed furniture")
    score += _fact(space.get("weekend_available"), 12,
                   "Weekend access available", "No weekend access")
    score += _fact(space.get("evening_available"), 6,
                   "Evening access available", "No evening access")
    score += _fact(space.get("recurring_available"), 12,
                   "Recurring booking possible", "No recurring booking")
    score += _fact(space.get("wifi"), 5, "Wi-Fi available", "No usable Wi-Fi")
    score += _fact(space.get("restroom_access"), 5,
                   "Restroom access", "No restroom access")
    score += _fact(space.get("ada_access"), 4,
                   "ADA/elevator access", "No ADA access")
    score += _fact(space.get("training_use_allowed"), 8,
                   "Training/education use allowed", "Training use not allowed")

    parking = str(space.get("parking_notes") or "").lower()
    if parking:
        checked += 1
        if any(k in parking for k in ("free", "lot", "ample", "garage",
                                      "available", "good")):
            score += 6
            reasons.append("Parking available")
        elif "street" in parking:
            score += 2
            reasons.append("Street parking only")

    # Price vs room budget policy.
    hourly = space.get("hourly_rate")
    daily = space.get("daily_rate")
    max_hourly = room_budget.get("max_hourly_rate")
    max_daily = room_budget.get("max_daily_rate")
    if hourly is not None and max_hourly:
        checked += 1
        if hourly <= max_hourly:
            score += 7
            reasons.append(f"Hourly rate ${hourly:.0f} within budget "
                           f"(max ${max_hourly:.0f})")
        else:
            score -= 8
            reasons.append(f"Hourly rate ${hourly:.0f} over budget "
                           f"(max ${max_hourly:.0f})")
    elif daily is not None and max_daily:
        checked += 1
        if daily <= max_daily:
            score += 7
            reasons.append(f"Daily rate ${daily:.0f} within budget "
                           f"(max ${max_daily:.0f})")
        else:
            score -= 8
            reasons.append(f"Daily rate ${daily:.0f} over budget "
                           f"(max ${max_daily:.0f})")

    if str(space.get("cancellation_policy") or "").strip():
        checked += 1
        score += 3
        reasons.append("Cancellation policy known")

    if flags:
        score = 0.0
        reasons.append("Hard-elimination rule triggered: " + ", ".join(flags))

    out = dict(space)
    out["classroom_fit_score"] = round(max(0.0, min(100.0, score)), 1)
    out["fit_reasons"] = reasons
    out["hard_elimination_flags"] = flags
    # Confidence tracks how much of the checklist has actually been verified.
    out["confidence_score"] = round(
        min(100.0, 100.0 * checked / total_checks), 1)
    return out


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def _location_candidate(row: Dict[str, Any], zip_code: str) -> Dict[str, Any]:
    active = row.get("active_status") == "active"
    return SharedSpaceCandidate(
        name=row.get("location_name", ""),
        source="allcpr_locations_import",
        address=row.get("address", ""),
        zip=row.get("zip", ""),
        space_type="MEETING_ROOM",
        capacity=int(row["capacity"]) if row.get("capacity") else None,
        daily_rate=None,
        floor_space_notes=row.get("room_notes", ""),
        parking_notes=row.get("parking_notes", ""),
        # An active ALLCPR location is a room ALLCPR already uses.
        recurring_available=True if active else None,
        training_use_allowed=True if active else None,
        outreach_status="CONFIRMED" if active else "NEW",
        notes=("Active ALLCPR location" if active
               else "Former/inactive ALLCPR location — re-verify before reuse"),
    ).to_dict()


def _commercial_candidate(row: Dict[str, Any], zip_code: str
                          ) -> Dict[str, Any]:
    fit = str(row.get("classroom_fit") or "").lower()
    return SharedSpaceCandidate(
        name=row.get("property_name") or row.get("address", ""),
        source="commercial_validation_csv",
        source_url=row.get("source_url", ""),
        address=row.get("address", ""),
        zip=str(row.get("zip") or zip_code),
        space_type="OTHER",
        floor_space_notes=f"classroom_fit={fit}" if fit else "",
        parking_notes=str(row.get("parking") or ""),
        notes=str(row.get("notes") or ""),
    ).to_dict()


# Institutional space signals from the enriched ZIP record.
_ZIP_SPACE_SIGNALS = (
    ("community_facility_count", "COMMUNITY_CENTER",
     "Community centers / libraries / churches ({count} facility(ies) in ZIP)"),
    ("training_school_count", "COLLEGE",
     "Colleges & training schools with rentable rooms ({count} in ZIP)"),
    ("nursing_school_count", "COLLEGE",
     "Nursing school classrooms ({count} in ZIP)"),
)


def _signal_spaces(zip_code: str, zip_row: Dict[str, Any]
                   ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key, stype, name_tpl in _ZIP_SPACE_SIGNALS:
        raw = zip_row.get(key)
        try:
            count = int(float(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            continue
        out.append(SharedSpaceCandidate(
            name=name_tpl.format(count=count),
            source="zip_enrichment_signal",
            zip=zip_code,
            space_type=stype,
            notes=("Institutional lead from enrichment data — identify "
                   "specific rooms, then verify CPR classroom fit."),
        ).to_dict())
    return out


def discover_space_candidates(
    zip_code: str,
    zip_row: Optional[Dict[str, Any]] = None,
    locations: Optional[List[Dict[str, Any]]] = None,
    commercial_rows: Optional[List[Dict[str, Any]]] = None,
    room_budget: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build the scored shared-space candidate list for one ZIP."""
    zip_code = str(zip_code).zfill(5)
    if locations is None:
        locations = load_locations_import()
    if room_budget is None:
        room_budget = load_room_budget_rules()
    candidates: List[Dict[str, Any]] = []
    for row in locations:
        if row.get("zip") == zip_code:
            candidates.append(_location_candidate(row, zip_code))
    for row in commercial_rows or []:
        candidates.append(_commercial_candidate(row, zip_code))
    if zip_row:
        candidates.extend(_signal_spaces(zip_code, zip_row))
    scored = [score_space_candidate(c, room_budget) for c in candidates]
    scored.sort(key=lambda c: (c.get("classroom_fit_score") or 0.0,
                               c.get("confidence_score") or 0.0), reverse=True)
    return scored


def top_space_leads(candidates: List[Dict[str, Any]], limit: int = 3
                    ) -> List[Dict[str, Any]]:
    return [c for c in candidates
            if not c.get("hard_elimination_flags")][:limit]


# --------------------------------------------------------------------------
# Readiness score (per the operational ladder)
# --------------------------------------------------------------------------
def classroom_readiness_score(candidates: List[Dict[str, Any]]
                              ) -> Dict[str, Any]:
    """Readiness ladder:

    100 confirmed recurring room
     80 multiple likely bookable rooms
     60 rooms found but CPR fit unknown
     40 rooms exist but expensive/limited
     20 weak room supply
      0 no room path found
    """
    usable = [c for c in candidates
              if not c.get("hard_elimination_flags")
              and c.get("outreach_status") != "REJECTED"]
    confirmed = [
        c for c in usable
        if c.get("outreach_status") in ("CONFIRMED", "GOOD_FIT")
        and (c.get("recurring_available") is True
             or c.get("outreach_status") == "CONFIRMED")
    ]
    likely = [c for c in usable if (c.get("classroom_fit_score") or 0) >= 55]
    named_rooms = [c for c in usable
                   if c.get("source") != "zip_enrichment_signal"]
    limited = [c for c in usable
               if c.get("outreach_status") in ("TOO_EXPENSIVE",
                                               "NO_WEEKEND_ACCESS",
                                               "BAD_FLOOR_SPACE")]
    signals = [c for c in usable if c.get("source") == "zip_enrichment_signal"]

    if confirmed:
        score, reason = 100.0, (
            f"{len(confirmed)} confirmed recurring room(s)")
    elif len(likely) >= 2:
        score, reason = 80.0, (
            f"{len(likely)} likely bookable room(s) with good CPR fit signals")
    elif named_rooms:
        score, reason = 60.0, (
            f"{len(named_rooms)} room(s) found but CPR classroom fit not yet "
            "verified")
    elif limited:
        score, reason = 40.0, (
            "Rooms exist but are expensive or limited "
            "(price/weekend/floor-space problems)")
    elif signals:
        score, reason = 20.0, (
            "Weak room supply — only institutional signals, no specific room")
    else:
        score, reason = 0.0, "No room path found"

    if confirmed:
        stage = STAGE_CONFIRMED
    elif named_rooms:
        stage = STAGE_CANDIDATE_FOUND
    elif signals:
        stage = STAGE_SIGNAL_ONLY
    elif candidates and not usable:
        stage = STAGE_BLOCKED
    else:
        stage = STAGE_NO_SIGNAL

    return {
        "score": score,
        "label": band_label(score),
        "stage": stage,
        "reason": reason,
        "counts": {
            "confirmed": len(confirmed),
            "likely_bookable": len(likely),
            "named_rooms": len(named_rooms),
            "signal_leads": len(signals),
            "eliminated": len(candidates) - len(usable),
        },
    }
