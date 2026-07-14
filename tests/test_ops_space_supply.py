"""Space supply engine: classroom fit scoring, hard eliminations, readiness."""
from __future__ import annotations

from app.ops import space_supply as ssup
from tests.ops_fixtures import STRONG_ZIP_ROW, confirmed_room, signal_room

BUDGET = {
    "max_hourly_rate": 75.0,
    "max_daily_rate": 400.0,
    "minimum_capacity": 8,
}


def test_good_room_scores_high():
    room = confirmed_room(hourly_rate=50.0, evening_available=True,
                          ada_access=True, parking_notes="Free lot",
                          floor_space_notes="Open floor for manikins",
                          cancellation_policy="48h notice")
    scored = ssup.score_space_candidate(room, BUDGET)
    assert scored["classroom_fit_score"] >= 80
    assert not scored["hard_elimination_flags"]
    assert scored["fit_reasons"]


def test_unknown_facts_add_nothing_but_do_not_eliminate():
    scored = ssup.score_space_candidate(signal_room(), BUDGET)
    assert scored["classroom_fit_score"] == 0.0
    assert not scored["hard_elimination_flags"]
    assert scored["confidence_score"] < 30


def test_hard_elimination_training_not_allowed():
    room = confirmed_room(training_use_allowed=False)
    scored = ssup.score_space_candidate(room, BUDGET)
    assert ssup.FLAG_TRAINING_NOT_ALLOWED in scored["hard_elimination_flags"]
    assert scored["classroom_fit_score"] == 0.0


def test_hard_elimination_no_access_control_or_camera_or_wifi():
    for field, flag in (
        ("access_control_possible", ssup.FLAG_NO_ACCESS_CONTROL),
        ("camera_allowed", ssup.FLAG_NO_CAMERA),
        ("wifi", ssup.FLAG_BAD_WIFI),
        ("recurring_available", ssup.FLAG_NO_RECURRING),
    ):
        scored = ssup.score_space_candidate(confirmed_room(**{field: False}),
                                            BUDGET)
        assert flag in scored["hard_elimination_flags"], field
        assert scored["classroom_fit_score"] == 0.0, field


def test_hard_elimination_from_staff_notes():
    room = confirmed_room(notes="Landlord uncooperative about weekend entry")
    scored = ssup.score_space_candidate(room, BUDGET)
    assert ssup.FLAG_UNCOOPERATIVE in scored["hard_elimination_flags"]


def test_over_budget_rate_penalized_not_eliminated():
    cheap = ssup.score_space_candidate(confirmed_room(hourly_rate=50.0),
                                       BUDGET)
    pricey = ssup.score_space_candidate(confirmed_room(hourly_rate=120.0),
                                        BUDGET)
    assert cheap["classroom_fit_score"] > pricey["classroom_fit_score"]
    assert not pricey["hard_elimination_flags"]


def test_readiness_ladder_confirmed_room_is_100():
    result = ssup.classroom_readiness_score([confirmed_room()])
    assert result["score"] == 100.0


def test_readiness_ladder_multiple_likely_rooms_is_80():
    rooms = [
        confirmed_room(outreach_status="REPLIED", classroom_fit_score=70.0),
        confirmed_room(name="Second Room", outreach_status="NEW",
                       classroom_fit_score=65.0),
    ]
    for r in rooms:
        r["classroom_fit_score"] = max(r["classroom_fit_score"], 60.0)
    result = ssup.classroom_readiness_score(rooms)
    assert result["score"] == 80.0


def test_readiness_ladder_room_found_fit_unknown_is_60():
    room = signal_room(source="commercial_validation_csv",
                       name="123 Main St meeting room")
    result = ssup.classroom_readiness_score([room])
    assert result["score"] == 60.0


def test_readiness_ladder_signals_only_is_20():
    result = ssup.classroom_readiness_score([signal_room()])
    assert result["score"] == 20.0


def test_readiness_ladder_no_rooms_is_0():
    result = ssup.classroom_readiness_score([])
    assert result["score"] == 0.0


def test_eliminated_rooms_do_not_count():
    bad = confirmed_room(training_use_allowed=False)
    bad = ssup.score_space_candidate(bad, BUDGET)
    result = ssup.classroom_readiness_score([bad])
    assert result["score"] == 0.0
    assert result["counts"]["eliminated"] == 1


def test_discovery_from_zip_row_and_locations():
    locations = [{
        "location_name": "Active Site", "address": "1 Main St",
        "zip": "95112", "active_status": "active",
        "courses_offered": ["ARC_BLS"], "capacity": 12.0, "rent_cost": 900.0,
        "parking_notes": "Free lot", "room_notes": "Open floor",
        "average_monthly_enrollment": 40.0,
    }]
    spaces = ssup.discover_space_candidates(
        "95112", zip_row=STRONG_ZIP_ROW, locations=locations,
        commercial_rows=[], room_budget=BUDGET)
    sources = {s["source"] for s in spaces}
    assert "allcpr_locations_import" in sources
    assert "zip_enrichment_signal" in sources
    active = next(s for s in spaces
                  if s["source"] == "allcpr_locations_import")
    assert active["outreach_status"] == "CONFIRMED"
