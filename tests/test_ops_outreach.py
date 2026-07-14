"""Outreach templates: drafts only, correct wording, CRM log entries."""
from __future__ import annotations

from app.ops.outreach_templates import (
    build_outreach_log_entry,
    generate_instructor_outreach,
    generate_space_outreach,
)
from tests.ops_fixtures import confirmed_room, named_instructor_lead


def test_instructor_draft_fills_name_place_and_courses():
    lead = named_instructor_lead()
    draft = generate_instructor_outreach(lead, zip_code="95112",
                                         staff_name="Alex Staff")
    assert "BLS / CPR Instructor Opportunity" in draft["subject"]
    assert "Hi Nursing Faculty Lead," in draft["body"]
    assert "Alex Staff" in draft["body"]
    assert "95112" in draft["body"]
    assert "Red Cross BLS" in draft["body"]
    # The six verification questions must all be present.
    for i in range(1, 7):
        assert f"{i}." in draft["body"]


def test_instructor_draft_keeps_placeholder_when_no_staff_name():
    draft = generate_instructor_outreach(named_instructor_lead())
    assert "[Staff Name]" in draft["body"]


def test_space_draft_fills_room_questions():
    room = confirmed_room()
    draft = generate_space_outreach(room, zip_code="95112",
                                    staff_name="Alex Staff", student_count=10)
    assert draft["subject"] == "Classroom Rental Inquiry for CPR/BLS Training"
    assert "approximately 10 students" in draft["body"]
    assert "CPR manikins" in draft["body"]
    for i in range(1, 9):
        assert f"{i}." in draft["body"]


def test_log_entry_is_draft_and_links_target():
    lead = named_instructor_lead()
    draft = generate_instructor_outreach(lead, zip_code="95112")
    entry = build_outreach_log_entry("INSTRUCTOR", lead, draft,
                                     created_by="Alex Staff")
    assert entry["status"] == "DRAFT"
    assert entry["sent_at"] is None
    assert entry["target_type"] == "INSTRUCTOR"
    assert entry["target_id"] == lead["id"]
    assert entry["message_template"] == draft["template_name"]
    assert draft["subject"] in entry["message_text"]
