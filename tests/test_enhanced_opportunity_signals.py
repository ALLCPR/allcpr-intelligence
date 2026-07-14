"""Tests for the enhanced (Phase-2) modeled-opportunity signal helpers."""
from __future__ import annotations

from app.config import COMPETITION_SATURATION_COUNT
from app.scoring.enhanced_opportunity_signals import (
    competition_gap_fraction,
    competition_gap_score,
    compute_enhanced_signals,
    count_competitors,
    count_healthcare_facilities,
    count_training_schools,
    density_per_sq_mile,
)


# --------------------------------------------------------------------------- #
# POI classification
# --------------------------------------------------------------------------- #
def test_healthcare_classification_by_type_and_keyword():
    places = [
        {"place_id": "1", "name": "County Hospital", "types": ["hospital"]},
        {"place_id": "2", "name": "CityMD Urgent Care", "types": ["doctor"]},
        {"place_id": "3", "name": "Downtown Medical Clinic", "types": []},
        {"place_id": "4", "name": "Pine Nursing Home", "types": []},
        {"place_id": "5", "name": "Joe's Coffee", "types": ["cafe"]},
    ]
    assert count_healthcare_facilities(places) == 4   # coffee shop excluded


def test_healthcare_dedupes_repeated_pois():
    places = [
        {"place_id": "1", "name": "County Hospital", "types": ["hospital"]},
        {"place_id": "1", "name": "County Hospital", "types": ["hospital"]},
    ]
    assert count_healthcare_facilities(places) == 1


def test_training_classification_is_cpr_bls_relevant():
    places = [
        {"place_id": "1", "name": "Bay Area School of Nursing"},
        {"place_id": "2", "name": "Red Cross CPR Training Center"},
        {"place_id": "3", "name": "Metro EMT Academy"},
        {"place_id": "4", "name": "Acme Medical Assistant Program"},
        {"place_id": "5", "name": "Lincoln Elementary School"},  # not relevant
    ]
    assert count_training_schools(places) == 4


def test_competitor_classification():
    places = [
        {"place_id": "1", "name": "ABC CPR & BLS Certification"},
        {"place_id": "2", "name": "First Aid Pros"},
        {"place_id": "3", "name": "Joe's Plumbing"},
    ]
    assert count_competitors(places) == 2


# --------------------------------------------------------------------------- #
# Density normalization
# --------------------------------------------------------------------------- #
def test_density_per_sq_mile_normalizes_by_area():
    assert density_per_sq_mile(10, 5.0) == 2.0
    assert density_per_sq_mile(0, 5.0) == 0.0


def test_density_is_none_without_real_area_or_count():
    assert density_per_sq_mile(10, None) is None
    assert density_per_sq_mile(10, 0) is None      # zero area never invents inf
    assert density_per_sq_mile(None, 5.0) is None


# --------------------------------------------------------------------------- #
# competition_gap_score formula (transparent + configurable)
# --------------------------------------------------------------------------- #
def test_competition_gap_approaches_one_with_no_competitors():
    assert competition_gap_fraction(0) == 1.0
    assert competition_gap_score(0) == 100.0


def test_competition_gap_approaches_zero_at_saturation():
    # Default saturation is 20 competitors.
    assert COMPETITION_SATURATION_COUNT == 20
    assert competition_gap_fraction(20) == 0.0
    assert competition_gap_score(20) == 0.0
    assert competition_gap_score(50) == 0.0        # clamped, never negative


def test_competition_gap_is_monotonic_and_configurable():
    assert (competition_gap_score(2)
            > competition_gap_score(8)
            > competition_gap_score(15))
    # A smaller saturation cap makes the same count score lower (saturates sooner).
    assert competition_gap_score(5, saturation_count=10) < competition_gap_score(5)


def test_competition_gap_none_when_count_missing():
    assert competition_gap_fraction(None) is None
    assert competition_gap_score(None) is None


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
def test_compute_enhanced_signals_from_counts():
    out = compute_enhanced_signals(
        land_sqmi=4.0,
        healthcare_count=12,
        training_count=2,
        competitor_count=4,
    )
    assert out["healthcare_facility_density"] == 3.0     # 12 / 4
    assert out["training_school_density"] == 0.5         # 2 / 4
    assert out["competition_gap_score"] == 80.0          # 100 * (1 - 4/20)
    dbg = out["debug"]
    assert dbg["land_sqmi"] == 4.0
    assert dbg["healthcare_facility_density"]["raw_count"] == 12
    assert dbg["competition_gap_score"]["competitor_count"] == 4


def test_compute_enhanced_signals_classifies_place_lists():
    out = compute_enhanced_signals(
        land_sqmi=2.0,
        healthcare_places=[
            {"place_id": "1", "name": "A Hospital", "types": ["hospital"]},
            {"place_id": "2", "name": "B Urgent Care"},
        ],
        training_places=[{"place_id": "3", "name": "C Nursing School"}],
        competitor_places=[{"place_id": "4", "name": "D CPR BLS class"}],
    )
    assert out["healthcare_facility_density"] == 1.0      # 2 / 2.0
    assert out["training_school_density"] == 0.5          # 1 / 2.0
    assert out["competition_gap_score"] == 95.0           # 100 * (1 - 1/20)


def test_compute_enhanced_signals_drops_density_without_area():
    out = compute_enhanced_signals(
        land_sqmi=None, healthcare_count=5, training_count=1, competitor_count=3)
    assert out["healthcare_facility_density"] is None
    assert out["training_school_density"] is None
    # The competition gap needs no area, so it still resolves.
    assert out["competition_gap_score"] == 85.0
