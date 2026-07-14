"""
Data models for the expansion-operations layer.

Follows the house pattern: dataclasses for normalized records, plain dicts on
the wire, string constants (not Enum classes) for statuses so records stay
JSON-round-trippable. Every record carries ``created_at`` / ``updated_at``
ISO timestamps.

Credential honesty rule: ``credential_status`` may only be ``VERIFIED`` after
a human recorded a verification. Discovery code must emit ``SIGNAL_ONLY`` or
``NEEDS_VERIFICATION``.

Sensitive-data rule: staff-only operational fields (door/lockbox/access codes,
Wi-Fi passwords, alarm codes) must never leave the server. ``scrub_sensitive``
is applied to every API payload as a last line of defense.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Courses
# --------------------------------------------------------------------------
AHA_BLS = "AHA_BLS"
ARC_BLS = "ARC_BLS"
ARC_CPR_FA_AED = "ARC_CPR_FA_AED"
OVERALL = "OVERALL"

COURSE_TYPES = (AHA_BLS, ARC_BLS, ARC_CPR_FA_AED)
READINESS_COURSE_TYPES = (OVERALL,) + COURSE_TYPES

COURSE_LABELS = {
    AHA_BLS: "AHA BLS",
    ARC_BLS: "ARC BLS",
    ARC_CPR_FA_AED: "ARC CPR/FA/AED",
    OVERALL: "Overall",
}

# --------------------------------------------------------------------------
# Instructor candidate vocabulary
# --------------------------------------------------------------------------
INSTRUCTOR_CANDIDATE_TYPES = (
    "AHA_BLS_INSTRUCTOR",
    "ARC_BLS_INSTRUCTOR",
    "ARC_CPR_INSTRUCTOR",
    "NURSING_PROFESSOR",
    "EMT_INSTRUCTOR",
    "PARAMEDIC_INSTRUCTOR",
    "FIRE_ACADEMY_INSTRUCTOR",
    "HOSPITAL_EDUCATOR",
    "CPR_BUSINESS_OWNER",
    "UNKNOWN_HEALTHCARE_TRAINER",
)

CREDENTIAL_STATUSES = (
    "UNKNOWN",
    "SIGNAL_ONLY",
    "NEEDS_VERIFICATION",
    "VERIFIED",
    "EXPIRED",
    "REJECTED",
)

LONG_TERM_INTEREST = ("UNKNOWN", "YES", "NO", "MAYBE")

INSTRUCTOR_OUTREACH_STATUSES = (
    "NEW",
    "NEEDS_REVIEW",
    "CONTACTED",
    "REPLIED",
    "INTERESTED",
    "NOT_INTERESTED",
    "CREDENTIAL_VERIFIED",
    "RATE_TOO_HIGH",
    "AVAILABLE",
    "CONFIRMED",
    "REJECTED",
)

# --------------------------------------------------------------------------
# Shared-space vocabulary
# --------------------------------------------------------------------------
SPACE_TYPES = (
    "COWORKING",
    "MEETING_ROOM",
    "HOTEL_MEETING_ROOM",
    "COMMUNITY_CENTER",
    "CHURCH",
    "LIBRARY",
    "COLLEGE",
    "MEDICAL_OFFICE",
    "ADULT_SCHOOL",
    "OTHER",
)

SPACE_OUTREACH_STATUSES = (
    "NEW",
    "NEEDS_REVIEW",
    "CONTACTED",
    "REPLIED",
    "AVAILABLE",
    "TOO_EXPENSIVE",
    "BAD_FLOOR_SPACE",
    "NO_WEEKEND_ACCESS",
    "GOOD_FIT",
    "CONFIRMED",
    "REJECTED",
)

# --------------------------------------------------------------------------
# Readiness / recommendation vocabulary
# --------------------------------------------------------------------------
RECOMMENDED_ACTIONS = (
    "NOT_READY_DEMAND_WEAK",
    "NOT_READY_NO_INSTRUCTOR",
    "NOT_READY_NO_SPACE",
    "RESEARCH_NEEDED",
    "INSTRUCTOR_OUTREACH_NEEDED",
    "SPACE_OUTREACH_NEEDED",
    "TEST_CLASS_READY",
    "RECURRING_CLASS_CANDIDATE",
    "PERMANENT_CENTER_CANDIDATE",
)

RECOMMENDED_ACTION_LABELS = {
    "NOT_READY_DEMAND_WEAK": "Not Ready — Demand Weak",
    "NOT_READY_NO_INSTRUCTOR": "Not Ready — No Instructor Path",
    "NOT_READY_NO_SPACE": "Not Ready — No Space Path",
    "RESEARCH_NEEDED": "Research Needed",
    "INSTRUCTOR_OUTREACH_NEEDED": "Instructor Outreach Needed",
    "SPACE_OUTREACH_NEEDED": "Space Outreach Needed",
    "TEST_CLASS_READY": "Test Class Ready",
    "RECURRING_CLASS_CANDIDATE": "Recurring Class Candidate",
    "PERMANENT_CENTER_CANDIDATE": "Permanent Center Candidate",
}

# Outreach / CRM log
OUTREACH_TARGET_TYPES = ("INSTRUCTOR", "SPACE")
OUTREACH_CHANNELS = ("EMAIL", "LINKEDIN", "PHONE", "SMS", "OTHER")

# Boss-friendly readiness bands (mirrors dashboard wording).
def band_label(score: Optional[float]) -> str:
    """Strong / Medium / Weak / No Signal wording for a 0..100 score."""
    if score is None:
        return "No Signal"
    if score >= 65:
        return "Strong"
    if score >= 40:
        return "Medium"
    if score > 0:
        return "Weak"
    return "No Signal"


# Lead-stage wording. A numeric band alone can mislead ("Medium · 50" when
# only public signals exist reads like instructors were found), so readiness
# also reports what kind of lead backs the score.
STAGE_CONFIRMED = "Confirmed"
STAGE_CANDIDATE_FOUND = "Candidate Found"
STAGE_SIGNAL_ONLY = "Signal Only"
STAGE_NO_SIGNAL = "No Signal"
STAGE_BLOCKED = "Blocked"

LEAD_STAGES = (STAGE_CONFIRMED, STAGE_CANDIDATE_FOUND, STAGE_SIGNAL_ONLY,
               STAGE_NO_SIGNAL, STAGE_BLOCKED)


def is_signal_lead(lead: Dict[str, Any]) -> bool:
    """True when a lead is a category/institutional signal ("Nursing program
    faculty…", "Community centers…") rather than a named person or a specific
    room. Signal leads must never be greeted by name or counted as candidates.
    """
    return (lead.get("source") == "zip_enrichment_signal"
            or lead.get("credential_status") == "SIGNAL_ONLY")


# --------------------------------------------------------------------------
# Instructor lead levels (1..6)
# --------------------------------------------------------------------------
# How strong an instructor lead is, from a bare institutional signal up to a
# confirmed long-term instructor. Key business rule: a PAST ALLCPR / Enrollware
# instructor (someone who has *actually taught* classes) is far stronger than a
# random professor lead, so it is its own level (3) above "named lead" (2).
LEVEL_INSTITUTIONAL_SIGNAL = 1
LEVEL_NAMED_LEAD = 2
LEVEL_PAST_INSTRUCTOR = 3
LEVEL_CONTACTED = 4
LEVEL_CREDENTIAL_VERIFIED = 5
LEVEL_CONFIRMED = 6

LEAD_LEVEL_LABELS = {
    LEVEL_INSTITUTIONAL_SIGNAL: "Institutional Signal",
    LEVEL_NAMED_LEAD: "Named Lead",
    LEVEL_PAST_INSTRUCTOR: "Past ALLCPR Instructor",
    LEVEL_CONTACTED: "Contacted / Interested",
    LEVEL_CREDENTIAL_VERIFIED: "Credential Verified",
    LEVEL_CONFIRMED: "Confirmed Instructor",
}

# Sources that mark a lead as a proven past ALLCPR/Enrollware instructor.
PAST_INSTRUCTOR_SOURCES = frozenset({
    "allcpr_past_instructor", "enrollware_performance",
})


def is_past_instructor(lead: Dict[str, Any]) -> bool:
    """True when the lead has a real ALLCPR/Enrollware teaching track record."""
    return bool(lead.get("past_instructor")
                or lead.get("source") in PAST_INSTRUCTOR_SOURCES)


def lead_level(lead: Dict[str, Any]) -> int:
    """Classify an instructor lead into levels 1..6 (higher = stronger).

    Progress states (contacted/verified/confirmed) win over intrinsic type, so
    a past instructor we've already verified reads as level 5, not 3.
    """
    status = str(lead.get("outreach_status") or "NEW").upper()
    cred = str(lead.get("credential_status") or "UNKNOWN").upper()
    if status == "CONFIRMED":
        return LEVEL_CONFIRMED
    if cred == "VERIFIED" or status in ("CREDENTIAL_VERIFIED", "AVAILABLE"):
        return LEVEL_CREDENTIAL_VERIFIED
    if status in ("CONTACTED", "REPLIED", "INTERESTED"):
        return LEVEL_CONTACTED
    if is_past_instructor(lead):
        return LEVEL_PAST_INSTRUCTOR
    if is_signal_lead(lead):
        return LEVEL_INSTITUTIONAL_SIGNAL
    if str(lead.get("name") or "").strip():
        return LEVEL_NAMED_LEAD
    return LEVEL_INSTITUTIONAL_SIGNAL


def lead_level_label(lead: Dict[str, Any]) -> str:
    return LEAD_LEVEL_LABELS[lead_level(lead)]


# --------------------------------------------------------------------------
# Sensitive staff-only data guard
# --------------------------------------------------------------------------
# Field-name fragments that indicate staff-only operational access data.
# Records built by this layer never define such fields, but imports and manual
# edits could introduce them; the API layer scrubs any key matching these.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(access[_ ]?code|door[_ ]?code|lock[_ ]?box|lockbox|alarm[_ ]?code|"
    r"gate[_ ]?code|key[_ ]?pad|keypad|wifi[_ ]?pass|wi[-_ ]?fi[_ ]?pass|"
    r"password|passcode|pin[_ ]?code|secret|"
    # API/integration secrets (Manatal, SMTP, write token). NB: intentionally
    # not "credential" — credential_status is a legitimate public field.
    r"api[_ ]?key|apikey|access[_ ]?token|auth[_ ]?token|authorization|"
    r"bearer|token)",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(str(key)))


def scrub_sensitive(value: Any) -> Any:
    """Recursively drop staff-only keys from a payload before it is served."""
    if isinstance(value, dict):
        return {
            k: scrub_sensitive(v)
            for k, v in value.items()
            if not is_sensitive_key(k)
        }
    if isinstance(value, list):
        return [scrub_sensitive(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass
class InstructorCandidate:
    """A possible instructor. Public data is a lead, never a certification."""

    id: str = field(default_factory=lambda: new_id("inst"))
    name: str = ""
    source: str = ""
    source_url: str = ""
    email: str = ""
    phone: str = ""
    organization: str = ""
    title: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance_miles: Optional[float] = None
    candidate_type: str = "UNKNOWN_HEALTHCARE_TRAINER"
    certification_signals: List[str] = field(default_factory=list)
    verified_certifications: List[str] = field(default_factory=list)
    credential_status: str = "UNKNOWN"
    courses_possible: List[str] = field(default_factory=list)
    travel_radius_miles: Optional[float] = None
    availability_notes: str = ""
    long_term_interest: str = "UNKNOWN"
    rate_notes: str = ""
    outreach_status: str = "NEW"
    confidence_score: float = 0.0
    score_reasons: List[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SharedSpaceCandidate:
    """A possible classroom/shared space. Fit facts stay None until checked."""

    id: str = field(default_factory=lambda: new_id("space"))
    name: str = ""
    source: str = ""
    source_url: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance_miles: Optional[float] = None
    space_type: str = "OTHER"
    hourly_rate: Optional[float] = None
    daily_rate: Optional[float] = None
    capacity: Optional[int] = None
    floor_space_notes: str = ""
    movable_tables_chairs: Optional[bool] = None
    weekend_available: Optional[bool] = None
    evening_available: Optional[bool] = None
    recurring_available: Optional[bool] = None
    parking_notes: str = ""
    wifi: Optional[bool] = None
    restroom_access: Optional[bool] = None
    ada_access: Optional[bool] = None
    camera_allowed: Optional[bool] = None
    access_control_possible: Optional[bool] = None
    signage_allowed: Optional[bool] = None
    training_use_allowed: Optional[bool] = None
    cancellation_policy: str = ""
    classroom_fit_score: float = 0.0
    fit_reasons: List[str] = field(default_factory=list)
    hard_elimination_flags: List[str] = field(default_factory=list)
    outreach_status: str = "NEW"
    confidence_score: float = 0.0
    notes: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ZipOperatingReadiness:
    """Per-ZIP, per-course operating readiness snapshot."""

    zip: str = ""
    course_type: str = OVERALL
    demand_score: Optional[float] = None
    instructor_readiness_score: Optional[float] = None
    aha_instructor_readiness_score: Optional[float] = None
    arc_instructor_readiness_score: Optional[float] = None
    classroom_readiness_score: Optional[float] = None
    commercial_feasibility_score: Optional[float] = None
    cannibalization_risk: Optional[float] = None
    final_operating_feasibility_score: Optional[float] = None
    recommended_action: str = "RESEARCH_NEEDED"
    recommended_action_label: str = RECOMMENDED_ACTION_LABELS["RESEARCH_NEEDED"]
    missing_requirements: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    explanation: str = ""
    next_steps: List[str] = field(default_factory=list)
    last_updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OutreachLog:
    """One outreach touch (draft, send record, reply) against a lead."""

    id: str = field(default_factory=lambda: new_id("out"))
    target_type: str = "INSTRUCTOR"
    target_id: str = ""
    channel: str = "EMAIL"
    message_template: str = ""
    message_text: str = ""
    sent_at: Optional[str] = None
    response_received: bool = False
    response_summary: str = ""
    next_followup_at: Optional[str] = None
    status: str = "DRAFT"
    created_by: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
