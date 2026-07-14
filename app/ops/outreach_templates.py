"""
Outreach draft generation for instructor and shared-space leads.

Drafts only — nothing here sends email or messages. A staff member copies the
draft, personalizes the placeholders that remain (staff name, final wording),
and sends it through their own channel; the OutreachLog records the touch.

Wording follows the approved operational templates. Placeholders that need
manual input stay in [brackets] so an unfinished draft is visually obvious.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ops.models import (
    AHA_BLS,
    ARC_BLS,
    ARC_CPR_FA_AED,
    OutreachLog,
    is_signal_lead,
)
from app.ops.recruiting_policy import INSTRUCTOR_SCREENING

INSTRUCTOR_TEMPLATE_NAME = "instructor_opportunity_v1"
SPACE_TEMPLATE_NAME = "classroom_rental_inquiry_v1"

_COURSE_WORDING = {
    AHA_BLS: "AHA BLS",
    ARC_BLS: "Red Cross BLS",
    ARC_CPR_FA_AED: "Red Cross CPR/FA/AED",
}


def _course_phrase(courses: Optional[List[str]]) -> str:
    names = [_COURSE_WORDING[c] for c in courses or [] if c in _COURSE_WORDING]
    if not names:
        return "AHA BLS / Red Cross BLS / Red Cross CPR/FA/AED"
    if len(names) == 1:
        return names[0]
    return " / ".join(names)


def _place_phrase(city: str, zip_code: str) -> str:
    if city and zip_code:
        return f"{city} ({zip_code})"
    return city or zip_code or "[City/ZIP]"


def _greeting_name(lead: Dict[str, Any], role_placeholder: str) -> str:
    """Greeting-safe name. Category/signal leads carry a description like
    "Nursing program faculty (3 nursing schools in ZIP)" in ``name`` — never
    greet with that; keep a bracketed placeholder until a real contact exists.
    """
    name = str(lead.get("name") or "").strip()
    if not name:
        return "[Name]"
    if is_signal_lead(lead):
        return role_placeholder
    return name


def generate_instructor_outreach(
    candidate: Dict[str, Any],
    zip_code: str = "",
    staff_name: str = "[Staff Name]",
    courses: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Instructor outreach draft (subject + body)."""
    name = _greeting_name(candidate, "[Program Coordinator]")
    city = candidate.get("city") or ""
    place = _place_phrase(city, zip_code or candidate.get("zip") or "")
    course_phrase = _course_phrase(courses
                                   or candidate.get("courses_possible"))
    eq = INSTRUCTOR_SCREENING["equipment_required"]
    subject = f"BLS / CPR Instructor Opportunity Near {city or place}"
    body = f"""Hi {name},

My name is {staff_name} from ALLCPR. We are expanding CPR and BLS classes near {place} and are looking for reliable long-term instructors who may be interested in teaching recurring classes.

We are especially looking for instructors certified in {course_phrase}. ALLCPR handles the classroom/venue and student registration and covers certification-card fees; instructors teach with their own training equipment (ideally {eq['adult_manikins']} sets of manikins — {eq['adult_manikins']} Adult, {eq['infant_manikins']} Infant, {eq['aed_trainers']} AED Trainers; {eq['minimum_acceptable_sets']} sets works for a strong fit).

Are you currently available to teach classes in this area? If yes, could you share:
1. Which instructor certifications you currently hold
2. Certification expiration dates
3. How many manikin sets you have (Adult / Infant / AED Trainer)
4. Preferred teaching days/times (we schedule interviews on weekdays)
5. Travel radius
6. Expected hourly or per-class rate
7. Whether you are open to recurring long-term classes

Thank you,
{staff_name}
ALLCPR"""
    return {
        "template_name": INSTRUCTOR_TEMPLATE_NAME,
        "subject": subject,
        "body": body,
        "suggested_channel": "EMAIL" if candidate.get("email") else "OTHER",
    }


def generate_space_outreach(
    space: Dict[str, Any],
    zip_code: str = "",
    staff_name: str = "[Staff Name]",
    student_count: int = 12,
) -> Dict[str, str]:
    """Shared-space rental inquiry draft (subject + body)."""
    contact = (str(space.get("contact_name") or "").strip()
               or _greeting_name(space, "[Facility Manager]"))
    place = _place_phrase(space.get("city") or "",
                          zip_code or space.get("zip") or "")
    subject = "Classroom Rental Inquiry for CPR/BLS Training"
    body = f"""Hi {contact},

My name is {staff_name} from ALLCPR. We are looking for a recurring classroom or meeting room near {place} for CPR and BLS training classes.

The room would need to support small groups of approximately {student_count} students, with enough open floor space for CPR manikins, movable chairs/tables if needed, restroom access, and weekend or evening availability.

Could you please let me know:
1. Hourly or daily rental rate
2. Weekend/evening availability
3. Room capacity
4. Whether tables/chairs can be moved
5. Parking situation
6. Whether recurring bookings are possible
7. Whether CPR/BLS training equipment is allowed
8. Cancellation policy

Thank you,
{staff_name}
ALLCPR"""
    return {
        "template_name": SPACE_TEMPLATE_NAME,
        "subject": subject,
        "body": body,
        "suggested_channel": "EMAIL",
    }


def build_outreach_log_entry(
    target_type: str,
    target: Dict[str, Any],
    draft: Dict[str, str],
    created_by: str = "",
) -> Dict[str, Any]:
    """OutreachLog record for a generated draft (status DRAFT, not sent)."""
    return OutreachLog(
        target_type=target_type,
        target_id=str(target.get("id") or ""),
        channel=draft.get("suggested_channel") or "EMAIL",
        message_template=draft.get("template_name", ""),
        message_text=f"Subject: {draft.get('subject', '')}\n\n"
                     f"{draft.get('body', '')}",
        status="DRAFT",
        created_by=created_by,
    ).to_dict()
