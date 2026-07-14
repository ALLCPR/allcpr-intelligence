"""Unit tests for business_feasibility + competition_detail math."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import allcpr_prices  # noqa: E402
from app.collectors.commercial_listings import CommercialOverride  # noqa: E402
from app.scoring import business_feasibility as bf  # noqa: E402
from app.scoring import competition_detail as cd  # noqa: E402


def test_rent_affordability_scaling():
    assert bf._rent_affordability(18.0) == 100.0   # cheap → 100
    assert bf._rent_affordability(72.0) == 0.0      # pricey → 0
    assert bf._rent_affordability(None) is None
    mid = bf._rent_affordability(45.0)
    assert 45 < mid < 55                            # midpoint ~50


def test_classroom_fit_uses_sqft():
    assert bf._classroom_fit(None) is None
    small = bf._classroom_fit(200)                  # ~5.7 students vs target 12
    big = bf._classroom_fit(1200)                   # comfortably above target
    assert small < big
    assert 0 <= small <= 100 and 0 <= big <= 100


def test_parking_score_from_notes():
    assert bf._parking_from_notes("") is None
    assert bf._parking_from_notes("no parking, street only") == 30.0
    good = bf._parking_from_notes("12 dedicated spaces in a lot")
    assert good is not None and good >= 80


def test_feasibility_no_override_withholds_space_scores(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", {})  # config default price
    out = bf.compute_feasibility(
        override=None, state="CA", accessibility_score=70.0,
        confidence_score=60.0, competition_pressure_score=40.0,
        area_score=65.0, revenue_low=1000.0, revenue_high=5000.0,
    )
    assert out.rent_score is None
    assert out.parking_score is None
    assert out.lease_readiness_score is None        # no validated space
    assert out.access_score == 70.0                 # proxy always present
    assert out.breakeven_students_per_month is not None
    assert out.data_basis == "none"


def test_feasibility_validated_override_scores_space(monkeypatch):
    monkeypatch.setattr(allcpr_prices, "_CACHE", {})
    ov = CommercialOverride(validation_status="validated", asking_rent=30.0,
                            square_feet=1500, parking_notes="20 spaces lot")
    out = bf.compute_feasibility(
        override=ov, state="CA", accessibility_score=70.0,
        confidence_score=60.0, competition_pressure_score=40.0,
        area_score=65.0, revenue_low=1000.0, revenue_high=5000.0,
    )
    assert out.rent_score is not None
    assert out.lease_readiness_score is not None
    assert out.data_basis == "validated_override"


def test_pressure_band_cutoffs():
    assert cd.pressure_band(None) == "Unknown"
    assert cd.pressure_band(10) == "Low"
    assert cd.pressure_band(40) == "Medium"
    assert cd.pressure_band(70) == "High"
    assert cd.pressure_band(90) == "Extreme"


def test_competition_detail_direct_vs_general():
    profile = {"competitors": [
        {"name": "Bay Area CPR & BLS Training", "types": ["health"]},
        {"name": "Red Cross First Aid", "types": []},
        {"name": "Joe's Coffee", "types": ["cafe"]},
    ]}
    summary = {"competitor_count_by_bucket_mi": {1: 1, 3: 2, 5: 3},
               "competitor_count_total": 3, "competitor_total_reviews": 100}
    out = cd.compute_competition_detail(profile, summary, 55.0)
    assert out.direct_competitors == 2
    assert out.general_competitors == 1
    assert out.band_0_1_mi == 1 and out.band_1_3_mi == 1 and out.band_3_5_mi == 1
    assert out.competition_pressure_band == "High"
