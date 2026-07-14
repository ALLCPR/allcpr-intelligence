"""
Recruiting & operating policy — the single, auditable home for the operational
rules ALLCPR runs by, transcribed from the internal SOPs (channel-management
SOP, Indeed handoff, specialist KPI form, cooperation accounting agreement,
Smart Manikin site-inspection SOP).

Keeping these as named constants (not scattered magic values) means the engine
speaks ALLCPR's actual playbook: the same instructor-equipment bar, the same
Indeed posting limits, the same enrollment targets, the same A–E grade bands,
the same priority expansion markets a human operator would apply.

Nothing secret lives here — no credentials, no PII, just policy numbers and
rules. The raw SOP documents stay in the gitignored ``Folder/``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# ALLCPR's own credentialing role (channel-management SOP §1.1)
# --------------------------------------------------------------------------
# ALLCPR is itself the Training Center / Provider instructors align to — so
# onboarding an instructor *is* the alignment step, not a separate hurdle.
ALLCPR_CREDENTIAL_ROLE = {
    "arc": "American Red Cross Primary Licensed Training Provider (PLTP)",
    "aha": "AHA Training Site",
    "note": ("ALLCPR is itself the Red Cross Licensed Training Provider and the "
             "AHA Training Site instructors align to — onboarding an instructor "
             "aligns them to ALLCPR, so no outside Training Center is needed."),
}

# --------------------------------------------------------------------------
# Instructor screening bar (Indeed handoff — "Policy / Candidate交流")
# --------------------------------------------------------------------------
INSTRUCTOR_SCREENING = {
    "equipment_required": {
        "adult_manikins": 4,
        "infant_manikins": 4,
        "aed_trainers": 4,
        "minimum_acceptable_sets": 3,
        "note": ("Prefer 4 sets of manikins (4 Adult, 4 Infant, 4 AED "
                 "Trainers). 3 sets acceptable for a strong candidate; "
                 "candidates with clear purchase intent may still interview."),
    },
    "interview_policy": "No interviews scheduled on weekends.",
    "cost_policy": {
        "allcpr_covers": ["certification-card fees", "venue/room fees"],
        "instructor_covers": ["travel", "equipment", "consumables"],
        "note": ("ALLCPR covers card + venue costs; the instructor covers "
                 "travel, their own equipment, and consumables."),
    },
}

# --------------------------------------------------------------------------
# Indeed posting rules (Indeed handoff — "indeed post&sponsor")
# --------------------------------------------------------------------------
INDEED_POLICY = {
    "one_post_per_zip": True,
    "free_posts_per_month": 3,
    "sponsor_min_daily_usd": 5.0,
    "sponsor_round_days": 7,
    "repost_cadence": "Weekly (post Wednesday, close the next Wednesday, one "
                      "week off, then repost) to keep Indeed exposure without "
                      "being flagged.",
    "primary_job_type": "AHA Instructor",
    "ats": "Manatal ATS (auto-replies + weekly repost)",
    "sponsor_approval": {
        "how": "Email request; Cornelia implements the sponsorship.",
        "required_fields": ["target ZIP code", "reason", "budget amount",
                            "sponsor days (7-day standard round)"],
        "report_back": ["resumes submitted", "interviews booked"],
    },
    "notes": [
        "One job post per ZIP code — multiple posts in one ZIP risk being "
        "flagged.",
        "Only 3 free job posts per month; beyond that a post must be "
        "sponsored.",
        "Sponsored daily spend is driven by exposure and can exceed the set "
        "budget.",
    ],
}

# --------------------------------------------------------------------------
# Enrollment / demand targets (specialist KPI form)
# --------------------------------------------------------------------------
ENROLLMENT_TARGETS = {
    "site_students_per_week": 25,        # AI+CPR site enrollment base
    "site_students_per_month": 108,      # 25/wk × ~4.33
    "specialist_new_students_per_week": 2,
    "enterprise_clients_per_month": 2,
}

# B2B customer types a specialist develops (KPI form §四 / demand side).
ENTERPRISE_CLIENT_TYPES = (
    "enterprise", "hospital / medical", "school", "kindergarten",
    "gym / fitness", "church", "government", "nonprofit",
)

# --------------------------------------------------------------------------
# Performance grade bands (specialist KPI form §九) — the company standard
# A–E scale, reused as a boss-familiar grade for instructor performance.
# --------------------------------------------------------------------------
GRADE_BANDS = (
    (95.0, "A+", "Excellent", 1.20),
    (90.0, "A", "Great", 1.00),
    (80.0, "B", "Good", 0.80),
    (70.0, "C", "Pass", 0.50),
    (60.0, "D", "Needs improvement", 0.0),
    (0.0, "E", "Below standard", 0.0),
)


def company_grade(score: Optional[float]) -> Dict[str, Any]:
    """Map a 0..100 score onto ALLCPR's A–E grade band."""
    if score is None:
        return {"grade": "—", "label": "No data", "bonus_multiplier": 0.0}
    for floor, grade, label, bonus in GRADE_BANDS:
        if score >= floor:
            return {"grade": grade, "label": label, "bonus_multiplier": bonus}
    return {"grade": "E", "label": "Below standard", "bonus_multiplier": 0.0}


# --------------------------------------------------------------------------
# Priority expansion markets (cooperation accounting agreement §11)
# --------------------------------------------------------------------------
PRIORITY_MARKETS = {
    # city (lower) -> state. The agreement names these as the next centers.
    "fremont": "CA",
    "milpitas": "CA",
    "santa clara": "CA",
    "san jose": "CA",
    "union city": "CA",
}


def is_priority_market(city: str = "", state: str = "") -> bool:
    """True when a city is a named ALLCPR priority expansion market."""
    c = str(city or "").strip().lower()
    s = str(state or "").strip().upper()
    if c not in PRIORITY_MARKETS:
        return False
    return not s or PRIORITY_MARKETS[c] == s


# Curated ZIPs for the priority markets, so a ZIP with no city attached can
# still be flagged. Not exhaustive — representative core ZIPs per city.
PRIORITY_MARKET_ZIPS: Dict[str, str] = {}
for _city, _zips in {
    "Fremont": ("94536", "94537", "94538", "94539", "94555"),
    "Milpitas": ("95035", "95036"),
    "Santa Clara": ("95050", "95051", "95052", "95053", "95054", "95055"),
    "San Jose": (
        "95110", "95111", "95112", "95113", "95116", "95117", "95118",
        "95119", "95120", "95121", "95122", "95123", "95124", "95125",
        "95126", "95127", "95128", "95129", "95130", "95131", "95132",
        "95133", "95134", "95135", "95136", "95138", "95139", "95148"),
    "Union City": ("94587",),
}.items():
    for _z in _zips:
        PRIORITY_MARKET_ZIPS[_z] = _city


def priority_market_for_zip(zip_code: str) -> Optional[str]:
    """City name when a ZIP is in a priority expansion market, else None."""
    return PRIORITY_MARKET_ZIPS.get(str(zip_code).strip().zfill(5))


# --------------------------------------------------------------------------
# Weekly site-health check (Smart Manikin ICPIS SOP — Weekly Site Check Report)
# --------------------------------------------------------------------------
SITE_HEALTH_CHECKLIST = (
    ("site_clean", "Site is clean"),
    ("trash_removed", "Trash has been removed"),
    ("supplies_available", "Disinfecting wipes / supplies available"),
    ("manikin_in_place", "Smart Manikin is in place"),
    ("tablet_charging", "iPad / tablet present and charging"),
    ("aed_bvm_mask_present", "AED pads / BVM / pocket mask present"),
    ("door_access_works", "Door / lockbox / access code works"),
    ("camera_online", "Camera is online and not blocked"),
    ("wifi_normal", "Wi-Fi appears normal"),
    ("signage_clear", "Signs / instructions are clear"),
    ("no_safety_hazards", "No safety hazards identified"),
)


def site_health_checklist() -> List[Dict[str, str]]:
    """The 11-item weekly site-health check, as an unchecked checklist."""
    return [{"key": k, "label": v, "done": False}
            for k, v in SITE_HEALTH_CHECKLIST]


def default_job_title(course_label: str) -> str:
    """Indeed job title for a course (the SOP's primary type is AHA Instructor)."""
    return f"{course_label} Instructor" if course_label else INDEED_POLICY[
        "primary_job_type"]
