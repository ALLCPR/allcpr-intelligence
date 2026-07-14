"""Automated rent-estimate tests — pressure index + anchor calibration."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scoring.rent_estimate import (  # noqa: E402
    apply_rent_estimates,
    build_rent_estimate,
    compute_rent_pressure_index,
    estimate_rent_per_sqft,
)


def _profile(income=None, nearby=0, competitors=0):
    counts = {f"cat{i}": 1 for i in range(nearby)}
    return {
        "economy": {"census": {"values": {
            "median_household_income": income,
        }}},
        "counts_5mi": counts,
        "competition_summary": {"competitor_count_total": competitors},
        "accessibility": {"signals": {}},
    }


# --------------------------------------------------------------------------- #
# Pressure index
# --------------------------------------------------------------------------- #

def test_higher_income_raises_pressure():
    low, _ = compute_rent_pressure_index(_profile(income=45_000))
    high, _ = compute_rent_pressure_index(_profile(income=150_000))
    assert high > low


def test_density_raises_pressure():
    sparse, _ = compute_rent_pressure_index(_profile(income=80_000, nearby=10))
    dense, _ = compute_rent_pressure_index(_profile(income=80_000, nearby=200))
    assert dense > sparse


def test_no_signals_returns_neutral():
    idx, notes = compute_rent_pressure_index({})
    assert idx == 50.0
    assert any("neutral" in n for n in notes)


def test_pressure_index_bounded_0_100():
    idx, _ = compute_rent_pressure_index(
        _profile(income=500_000, nearby=1000, competitors=200))
    assert 0 <= idx <= 100


# --------------------------------------------------------------------------- #
# Anchor calibration
# --------------------------------------------------------------------------- #

def test_no_anchor_returns_none():
    assert estimate_rent_per_sqft(60.0, []) is None


def test_single_anchor_scales_proportionally():
    # anchor: index 50 → $40/sqft. index 100 → ~$80.
    est = estimate_rent_per_sqft(100.0, [(50.0, 40.0)])
    assert abs(est - 80.0) < 0.01


def test_two_anchors_linear_fit():
    # (40 → $30), (80 → $70): slope 1.0, intercept -10. index 60 → $50.
    est = estimate_rent_per_sqft(60.0, [(40.0, 30.0), (80.0, 70.0)])
    assert abs(est - 50.0) < 0.5


def test_build_rent_estimate_labels_confidence():
    prof = _profile(income=100_000, nearby=80, competitors=15)
    no_anchor = build_rent_estimate(prof, anchors=[])
    assert no_anchor.confidence == "estimated"
    assert no_anchor.estimated_rent_per_sqft is None

    anchored = build_rent_estimate(prof, anchors=[(50.0, 45.0)])
    assert anchored.confidence == "estimated_anchored"
    assert anchored.estimated_rent_per_sqft is not None


# --------------------------------------------------------------------------- #
# Cohort post-pass
# --------------------------------------------------------------------------- #

def test_apply_rent_estimates_uses_cited_anchor_from_cohort():
    # Candidate A has a cited override; B and C don't. B/C should still get
    # a dollar estimate calibrated from A.
    a_profile = _profile(income=120_000, nearby=100, competitors=20)
    a_profile["economy"]["real_estate"] = {
        "values": {"rent_per_sqft_annual": 60.0}}
    b_profile = _profile(income=70_000, nearby=40, competitors=8)
    c_profile = _profile(income=50_000, nearby=20, competitors=4)
    ranked = [
        (a_profile, {"rent": {}}),
        (b_profile, {"rent": {}}),
        (c_profile, {"rent": {}}),
    ]
    apply_rent_estimates(ranked)
    for _, scored in ranked:
        est = scored["rent_estimate"]
        assert est["anchor_count"] == 1
        assert est["estimated_rent_per_sqft"] is not None
        assert est["confidence"] == "estimated_anchored"
    # Cheaper-area candidate C should estimate lower rent than pricey A.
    a_rent = ranked[0][1]["rent_estimate"]["estimated_rent_per_sqft"]
    c_rent = ranked[2][1]["rent_estimate"]["estimated_rent_per_sqft"]
    assert c_rent < a_rent


def test_apply_rent_estimates_no_anchor_index_only():
    ranked = [
        (_profile(income=80_000, nearby=50), {"rent": {}}),
        (_profile(income=60_000, nearby=20), {"rent": {}}),
    ]
    apply_rent_estimates(ranked)
    for _, scored in ranked:
        est = scored["rent_estimate"]
        assert est["estimated_rent_per_sqft"] is None
        assert est["confidence"] == "estimated"
        assert 0 <= est["rent_pressure_index"] <= 100
