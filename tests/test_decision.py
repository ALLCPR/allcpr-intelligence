"""Tests for the ZIP decision logic."""
from __future__ import annotations

from app.scoring import decision as d

READY = {"commercial_validated": True, "commercial_ready": True,
         "available_space_count": 1, "parking_summary": "Yes",
         "classroom_fit_summary": "Good"}
NOT_READY = {"commercial_validated": True, "commercial_ready": False,
             "available_space_count": 0}


def test_historical_strong():
    out = d.decide("historical", overall_score=85, course_norm=0.8)
    assert out["decision_label"] == d.STRONG
    assert out["next_steps"]


def test_historical_possible_and_watching_and_lower():
    assert d.decide("historical", overall_score=80, course_norm=0.2)["decision_label"] == d.POSSIBLE
    assert d.decide("historical", overall_score=30, course_norm=0.8)["decision_label"] == d.WATCHING
    assert d.decide("historical", overall_score=10, course_norm=0.1)["decision_label"] == d.LOWER


def test_modeled_baseline_bands_and_validation_flag():
    strong = d.decide("modeled", overall_score=75, course_norm=0.7)
    assert strong["decision_label"] == d.STRONG
    assert any("Modeled estimate" in f for f in strong["risk_flags"])
    assert d.decide("modeled", overall_score=50, course_norm=0.4)["decision_label"] == d.POSSIBLE
    assert d.decide("modeled", overall_score=20, course_norm=0.1)["decision_label"] == d.LOWER


def test_commercial_upgrade_from_strong():
    base = d.decide("historical", overall_score=85, course_norm=0.8)
    assert base["decision_label"] == d.STRONG
    up = d.decide("historical", overall_score=85, course_norm=0.8, commercial=READY)
    assert up["decision_label"] == d.COMMERCIAL


def test_no_upgrade_when_commercial_not_ready():
    out = d.decide("historical", overall_score=85, course_norm=0.8, commercial=NOT_READY)
    assert out["decision_label"] == d.STRONG
    assert any("Commercial space not validated" in f for f in out["risk_flags"])


def test_enriched_modeled_with_commercial():
    out = d.decide("modeled", overall_score=78, course_norm=0.8,
                   enrichment_tier="commercial", commercial=READY)
    assert out["decision_label"] == d.COMMERCIAL


def test_low_confidence_adds_risk_flag():
    out = d.decide("modeled", overall_score=50, course_norm=0.4, data_confidence="missing")
    assert any("Low data confidence" in f for f in out["risk_flags"])


def test_no_open_now_language_anywhere():
    forbidden = ("open now", "lease-ready", "ready to lease", "sign the lease")
    for layer in ("historical", "modeled"):
        for score in (10, 45, 75, 95):
            out = d.decide(layer, overall_score=score, course_norm=0.9,
                           enrichment_tier="commercial", commercial=READY)
            blob = " ".join([out["decision_label"], out["decision_reason"],
                             *out["next_steps"], *out["risk_flags"]]).lower()
            for phrase in forbidden:
                assert phrase not in blob
