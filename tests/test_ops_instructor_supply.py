"""Instructor supply engine: candidate scoring + readiness ladder."""
from __future__ import annotations

from app.ops import instructor_supply as isup
from tests.ops_fixtures import (
    STRONG_ZIP_ROW,
    confirmed_instructor,
    named_instructor_lead,
    signal_instructor_lead,
)


def test_verified_close_contactable_scores_higher_than_signal():
    verified = isup.score_instructor_candidate(confirmed_instructor())
    signal = isup.score_instructor_candidate(signal_instructor_lead())
    assert verified["confidence_score"] > signal["confidence_score"]
    assert verified["score_reasons"]


def test_explicit_bls_signal_beats_generic_healthcare():
    explicit = isup.score_instructor_candidate(named_instructor_lead(
        certification_signals=["BLS instructor"]))
    generic = isup.score_instructor_candidate(named_instructor_lead(
        certification_signals=["registered nurse"]))
    assert explicit["confidence_score"] > generic["confidence_score"]


def test_outside_travel_radius_penalized():
    near = isup.score_instructor_candidate(named_instructor_lead(
        distance_miles=5.0))
    far = isup.score_instructor_candidate(named_instructor_lead(
        distance_miles=45.0))
    assert near["confidence_score"] > far["confidence_score"]


def test_rejected_candidate_scores_zero():
    rejected = isup.score_instructor_candidate(named_instructor_lead(
        credential_status="REJECTED"))
    assert rejected["confidence_score"] == 0.0


def test_readiness_ladder_confirmed_is_100():
    result = isup.instructor_readiness_score([confirmed_instructor()])
    assert result["score"] == 100.0


def test_readiness_ladder_allcpr_nearby_is_80():
    cand = confirmed_instructor(credential_status="NEEDS_VERIFICATION",
                                outreach_status="NEW")
    result = isup.instructor_readiness_score([cand])
    assert result["score"] == 80.0


def test_readiness_ladder_strong_named_is_65():
    cand = isup.score_instructor_candidate(named_instructor_lead())
    result = isup.instructor_readiness_score([cand])
    assert result["score"] == 65.0


def test_readiness_ladder_signals_only_is_50():
    signals = [signal_instructor_lead(), signal_instructor_lead(
        name="Hospital educators (2 hospitals)")]
    result = isup.instructor_readiness_score(signals)
    assert result["score"] == 50.0


def test_readiness_ladder_single_weak_signal_is_25():
    result = isup.instructor_readiness_score([signal_instructor_lead()])
    assert result["score"] == 25.0


def test_readiness_ladder_no_candidates_is_0():
    result = isup.instructor_readiness_score([])
    assert result["score"] == 0.0
    assert result["label"] == "No Signal"


def test_course_filter_excludes_wrong_course():
    arc_only = confirmed_instructor(courses=["ARC_BLS"])
    result = isup.instructor_readiness_score([arc_only], course="AHA_BLS")
    assert result["score"] == 0.0


def test_discovery_emits_signal_leads_from_zip_row_never_verified():
    candidates = isup.discover_instructor_candidates(
        "95112", zip_row=STRONG_ZIP_ROW, roster=[])
    assert candidates
    # Public/enrichment discovery must never claim a verified credential.
    assert all(c["credential_status"] == "SIGNAL_ONLY" for c in candidates)
    types = {c["candidate_type"] for c in candidates}
    assert "NURSING_PROFESSOR" in types
    assert "HOSPITAL_EDUCATOR" in types
    assert "CPR_BUSINESS_OWNER" in types


def test_discovery_includes_roster_within_radius():
    roster = [{
        "name": "Jane Roster", "city": "San Jose", "state": "CA",
        "zip": "95112", "courses": ["AHA_BLS"], "certifications": ["AHA BLS"],
        "expiration_dates": [], "travel_radius_miles": 25.0,
        "availability": "", "pay_rate": "", "reliability_notes": "",
        "languages": [], "long_term_interest": "YES", "verified": False,
    }]
    candidates = isup.discover_instructor_candidates(
        "95112", zip_row=None, roster=roster)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["source"] == "allcpr_internal_import"
    assert cand["credential_status"] == "NEEDS_VERIFICATION"
    assert cand["courses_possible"] == ["AHA_BLS"]


def test_grouping_by_course():
    cands = [confirmed_instructor(courses=["AHA_BLS"]),
             named_instructor_lead(courses=["ARC_CPR_FA_AED"])]
    grouped = isup.group_by_course(cands)
    assert len(grouped["AHA_BLS"]) == 1
    assert len(grouped["ARC_CPR_FA_AED"]) == 1
    assert len(grouped["ARC_BLS"]) == 0
