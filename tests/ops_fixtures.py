"""Shared fixture builders for the expansion-operations (ops) tests."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ops.models import InstructorCandidate, SharedSpaceCandidate

STRONG_ZIP_ROW: Dict[str, Any] = {
    "zip": "95112",
    "market_demand_score": 82.0,
    "overall": 80.0,
    "aha_bls_score": 78.0,
    "arc_bls_score": 81.0,
    "arc_cpr_score": 84.0,
    "bls_demand": 76.0,
    "cpr_demand": 85.0,
    "hospital_count": 4,
    "nursing_school_count": 3,
    "training_school_count": 6,
    "competitor_count": 5,
    "community_facility_count": 8,
    "competition_saturation_penalty": 0.0,
    "competition_risk_level": "low_to_moderate",
    "historical_status": "no_allcpr_history",
}

WEAK_ZIP_ROW: Dict[str, Any] = {
    "zip": "99999",
    "market_demand_score": 22.0,
    "overall": 25.0,
    "hospital_count": 0,
    "nursing_school_count": 0,
    "training_school_count": 0,
    "competitor_count": 0,
    "historical_status": "no_allcpr_history",
}


def confirmed_instructor(courses: Optional[List[str]] = None,
                         **overrides: Any) -> Dict[str, Any]:
    """A verified, confirmed instructor ready to teach."""
    cand = InstructorCandidate(
        name="Confirmed Instructor",
        source="allcpr_internal_import",
        organization="ALLCPR",
        zip="95112",
        distance_miles=5.0,
        candidate_type="AHA_BLS_INSTRUCTOR",
        certification_signals=["AHA BLS Instructor"],
        verified_certifications=["AHA BLS Instructor"],
        credential_status="VERIFIED",
        courses_possible=courses or ["AHA_BLS", "ARC_BLS"],
        travel_radius_miles=20.0,
        outreach_status="CONFIRMED",
    ).to_dict()
    cand.update(overrides)
    return cand


def named_instructor_lead(courses: Optional[List[str]] = None,
                          **overrides: Any) -> Dict[str, Any]:
    """A strong named candidate, not yet contacted or verified."""
    cand = InstructorCandidate(
        name="Nursing Faculty Lead",
        source="public_signal",
        organization="Community College",
        email="lead@example.edu",
        zip="95112",
        distance_miles=4.0,
        candidate_type="NURSING_PROFESSOR",
        certification_signals=["BLS instructor", "ACLS"],
        credential_status="NEEDS_VERIFICATION",
        courses_possible=courses or ["ARC_BLS", "ARC_CPR_FA_AED"],
        confidence_score=55.0,
    ).to_dict()
    cand.update(overrides)
    return cand


def signal_instructor_lead(**overrides: Any) -> Dict[str, Any]:
    """Institutional signal only — no named instructor."""
    cand = InstructorCandidate(
        name="Nursing program faculty (3 nursing schools in ZIP)",
        source="zip_enrichment_signal",
        zip="95112",
        distance_miles=0.0,
        candidate_type="NURSING_PROFESSOR",
        certification_signals=["nursing_school_count=3"],
        credential_status="SIGNAL_ONLY",
        courses_possible=["ARC_BLS", "ARC_CPR_FA_AED", "AHA_BLS"],
    ).to_dict()
    cand.update(overrides)
    return cand


def confirmed_room(**overrides: Any) -> Dict[str, Any]:
    """A confirmed recurring classroom."""
    room = SharedSpaceCandidate(
        name="Confirmed Training Room",
        source="allcpr_locations_import",
        zip="95112",
        space_type="MEETING_ROOM",
        capacity=12,
        movable_tables_chairs=True,
        weekend_available=True,
        recurring_available=True,
        wifi=True,
        restroom_access=True,
        training_use_allowed=True,
        outreach_status="CONFIRMED",
        classroom_fit_score=85.0,
    ).to_dict()
    room["hard_elimination_flags"] = []
    room.update(overrides)
    return room


def signal_room(**overrides: Any) -> Dict[str, Any]:
    """Institutional room signal only."""
    room = SharedSpaceCandidate(
        name="Community centers (2 in ZIP)",
        source="zip_enrichment_signal",
        zip="95112",
        space_type="COMMUNITY_CENTER",
    ).to_dict()
    room["hard_elimination_flags"] = []
    room.update(overrides)
    return room
