"""Tests for instructor→ZIP matching and the lead-level (1..6) taxonomy.

Core business rule under test: a PAST ALLCPR/Enrollware instructor (proven
track record) outranks any un-taught professor lead, and CRM/ATS progress
(contacted / verified / confirmed) promotes a lead to a higher level.
"""
from __future__ import annotations

from app.ops.instructor_matching import match_instructors_for_zip
from app.ops.models import (
    LEVEL_CONTACTED,
    LEVEL_INSTITUTIONAL_SIGNAL,
    LEVEL_NAMED_LEAD,
    LEVEL_PAST_INSTRUCTOR,
    lead_level,
)


def _perf(name, home_zip, courses, score=60.0, tier="Solid", students=300):
    return {"name": name, "home_zip": home_zip, "home_city": "Hayward",
            "home_state": "CA", "proven_courses": list(courses),
            "performance_score": score, "performance_tier": tier,
            "students": students, "classes": 40, "last_taught": "2026-05-01",
            "company_grade": "B"}


def _lead(name, source, **kw):
    base = {"id": kw.pop("id", None), "name": name, "source": source, "zip": "",
            "credential_status": "SIGNAL_ONLY" if source == "zip_enrichment_signal"
            else "NEEDS_VERIFICATION",
            "outreach_status": "NEW", "courses_possible": [],
            "distance_miles": None}
    base.update(kw)
    return base


def _near(_home, _target):   # fake distance fn: everyone 5 mi away
    return 5.0


def test_past_instructor_outranks_professor_lead():
    perf = [_perf("Jane Doe", "94541", ["AHA_BLS"])]
    stored = [_lead("Prof John Smith", "live_scrape", zip="94541",
                    distance_miles=3.0)]
    res = match_instructors_for_zip("94541", stored_leads=stored,
                                    performance_rows=perf, distance_fn=_near)
    top = res["best_instructor_path"][0]
    assert top["name"] == "Jane Doe"
    assert top["lead_level"] == LEVEL_PAST_INSTRUCTOR
    assert top["past_instructor"] is True
    assert res["recommended_action"] == "CONTACT_PAST_INSTRUCTOR"


def test_crm_progress_promotes_matched_past_instructor():
    # Same person exists as a stored lead already CONTACTED — level jumps to 4.
    perf = [_perf("Jane Doe", "94541", ["AHA_BLS"])]
    stored = [_lead("Jane Doe", "allcpr_internal_import", zip="94541",
                    outreach_status="CONTACTED", distance_miles=2.0)]
    res = match_instructors_for_zip("94541", stored_leads=stored,
                                    performance_rows=perf, distance_fn=_near)
    jane = res["best_instructor_path"][0]
    assert jane["past_instructor"] is True          # track record merged in
    assert jane["lead_level"] == LEVEL_CONTACTED     # progress wins over type
    assert res["recommended_action"] == "ADVANCE_IN_MANATAL"


def test_signal_lead_is_level_one_and_ranks_last():
    stored = [
        _lead("Nursing program faculty (2 schools)", "zip_enrichment_signal",
              zip="94541", distance_miles=0.0),
        _lead("Prof A", "live_scrape", zip="94541", distance_miles=1.0),
    ]
    res = match_instructors_for_zip("94541", stored_leads=stored,
                                    performance_rows=[],
                                    distance_fn=lambda h, t: None)
    levels = [c["lead_level"] for c in res["best_instructor_path"]]
    assert levels[0] == LEVEL_NAMED_LEAD
    assert levels[-1] == LEVEL_INSTITUTIONAL_SIGNAL


def test_far_instructor_surfaces_as_expanded_search_fallback():
    # Nothing within radius → the nearest past instructors still show, honestly
    # labeled beyond_radius, and the action says the nearest one is far.
    perf = [_perf("Far Guy", "99999", ["AHA_BLS"])]
    res = match_instructors_for_zip("94541", stored_leads=[],
                                    performance_rows=perf, radius_miles=25.0,
                                    distance_fn=lambda h, t: 40.0)
    assert res["expanded_search"] is True
    assert res["count"] == 1
    top = res["best_instructor_path"][0]
    assert top["beyond_radius"] is True and top["distance_miles"] == 40.0
    assert res["recommended_action"] == "CONTACT_NEAREST_FAR"
    assert "40.0 mi" in res["explanation"]


def test_expanded_search_keeps_nearest_three_only():
    perf = [_perf(f"P{i}", "99999", ["AHA_BLS"]) for i in range(6)]
    dists = iter([300.0, 100.0, 500.0, 50.0, 400.0, 200.0])
    lookup = {}
    def fn(h, t):
        # one distance per performance row, keyed by call order via name merge
        return next(dists)
    res = match_instructors_for_zip("94541", stored_leads=[],
                                    performance_rows=perf, radius_miles=25.0,
                                    distance_fn=fn)
    got = [c["distance_miles"] for c in res["best_instructor_path"]]
    assert got == [50.0, 100.0, 200.0]      # nearest 3, sorted
    assert res["expanded_search"] is True


def test_no_expansion_when_real_nearby_exists():
    perf = [_perf("Near", "94541", ["AHA_BLS"]),
            _perf("Far", "99999", ["AHA_BLS"])]
    res = match_instructors_for_zip(
        "94541", stored_leads=[], performance_rows=perf, radius_miles=25.0,
        distance_fn=lambda h, t: 3.0 if h == "94541" else 400.0)
    assert res["expanded_search"] is False
    names = [c["name"] for c in res["best_instructor_path"]]
    assert names == ["Near"]                 # far one stays out


def test_course_filter_keeps_signals_drops_offcourse_named():
    perf = [_perf("AHA Ace", "94541", ["AHA_BLS"]),
            _perf("ARC Ann", "94541", ["ARC_BLS"])]
    stored = [_lead("Signal", "zip_enrichment_signal", zip="94541",
                    distance_miles=0.0)]
    res = match_instructors_for_zip("94541", course="AHA_BLS",
                                    stored_leads=stored, performance_rows=perf,
                                    distance_fn=_near)
    names = [c["name"] for c in res["best_instructor_path"]]
    assert "AHA Ace" in names
    assert "ARC Ann" not in names        # filtered out by course
    assert "Signal" in names             # signals kept (seed sourcing)


def test_past_instructor_not_auto_credential_verified():
    perf = [_perf("Jane", "94541", ["AHA_BLS"])]
    res = match_instructors_for_zip("94541", stored_leads=[],
                                    performance_rows=perf, distance_fn=_near)
    jane = res["best_instructor_path"][0]
    assert jane["credential_status"] == "NEEDS_VERIFICATION"
    assert jane["lead_level"] == LEVEL_PAST_INSTRUCTOR
    assert "normalized_name" in jane


def test_lead_level_confirmed_beats_everything():
    assert lead_level({"name": "X", "outreach_status": "CONFIRMED"}) == 6
    assert lead_level({"name": "X", "credential_status": "VERIFIED"}) == 5
    assert lead_level({"name": "X", "source": "enrollware_performance"}) == 3
