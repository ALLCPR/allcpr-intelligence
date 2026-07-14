"""Tests for the national ZIP modeled-opportunity score."""
from __future__ import annotations

from app.config import ZIP_MODELED_WEIGHTS_BLS, ZIP_MODELED_WEIGHTS_CPR
from app.scoring.zip_modeled_opportunity import (
    BASELINE_SIGNALS,
    compute_zip_modeled_opportunity,
    signal_weight_breakdown,
)

# The three enhanced (enrichment) signals that fill the BLS tilt's remaining
# 0.20 of weight on top of the 0.80 baseline.
ENHANCED_SIGNALS = (
    "healthcare_facility_density",
    "training_school_density",
    "competition_gap_score",
)


def _strong_baseline():
    return {
        "population": 35_000,
        "population_density": 6_000,
        "median_household_income": 120_000,
        "working_age_share": 0.79,
        "employment_rate": 0.73,
        "bachelors_or_higher_share": 0.52,
        "healthcare_employment_share": 0.24,
    }


def _full_enhanced():
    # Real, in-bounds enhanced values (per-sq-mile densities + 0..100 gap).
    return {
        "healthcare_facility_density": 8.0,
        "training_school_density": 3.0,
        "competition_gap_score": 70.0,
    }


def test_weight_maps_sum_to_one():
    assert abs(sum(ZIP_MODELED_WEIGHTS_BLS.values()) - 1.0) < 1e-6
    assert abs(sum(ZIP_MODELED_WEIGHTS_CPR.values()) - 1.0) < 1e-6


def test_scores_in_range_and_tiered():
    out = compute_zip_modeled_opportunity(_strong_baseline())
    for key in ("overall", "bls_demand", "cpr_demand"):
        assert 0.0 <= out[key] <= 100.0
    assert out["tier"] == "baseline"
    assert out["data_quality"]["confidence"] == "ok"
    assert out["recommendation"]
    assert out["plain_english_summary"]
    assert out["recommended_next_action"]
    assert out["score_drivers"]
    assert isinstance(out["score_weaknesses"], list)
    assert "requires_commercial_validation" in out["risk_flags"]
    assert "modeled_only" in out["risk_flags"]


def test_strong_zip_outscores_weak_zip():
    strong = compute_zip_modeled_opportunity(_strong_baseline())["overall"]
    weak = compute_zip_modeled_opportunity({
        "population": 800,
        "population_density": 60,
        "median_household_income": 41_000,
        "working_age_share": 0.56,
        "employment_rate": 0.51,
        "bachelors_or_higher_share": 0.16,
        "healthcare_employment_share": 0.05,
    })["overall"]
    assert strong > weak


def test_healthcare_share_lifts_bls_more_than_cpr():
    base = _strong_baseline()
    low = dict(base, healthcare_employment_share=0.05)
    high = dict(base, healthcare_employment_share=0.25)
    bls_gain = (compute_zip_modeled_opportunity(high)["bls_demand"]
                - compute_zip_modeled_opportunity(low)["bls_demand"])
    cpr_gain = (compute_zip_modeled_opportunity(high)["cpr_demand"]
                - compute_zip_modeled_opportunity(low)["cpr_demand"])
    assert bls_gain > cpr_gain > 0


def test_missing_signals_renormalize_without_crash():
    # Only two signals present — must still score, flagged low-confidence.
    out = compute_zip_modeled_opportunity({
        "population": 20_000,
        "healthcare_employment_share": 0.20,
    })
    assert out["overall"] is not None
    assert 0 <= out["overall"] <= 100
    assert out["data_quality"]["confidence"] in ("partial", "missing")


def test_empty_features_returns_none_not_zero():
    out = compute_zip_modeled_opportunity({})
    assert out["overall"] is None
    assert out["bls_demand"] is None
    assert "Insufficient" in out["recommendation"]
    assert "Insufficient data" in out["plain_english_summary"]
    assert out["recommended_next_action"] == "Insufficient data — add enrichment signals before ranking."


def test_enrichment_signals_flip_tier():
    feats = dict(_strong_baseline(),
                 healthcare_facility_density=12,
                 competition_gap_score=85)
    out = compute_zip_modeled_opportunity(feats)
    assert out["tier"] == "enriched"
    assert out["data_quality"]["enrichment_present"] is True


def test_none_values_treated_as_missing():
    feats = {s: None for s in BASELINE_SIGNALS}
    out = compute_zip_modeled_opportunity(feats)
    assert out["overall"] is None


# --------------------------------------------------------------------------- #
# Enhanced-signal renormalization: the BLS denominator (sum of used weights)
# moves from 0.80 (baseline only) to 1.00 ONLY when all three enhanced signals
# are present; any missing enhanced signal is excluded and weights renormalize.
# --------------------------------------------------------------------------- #
def test_baseline_only_denominator_is_080():
    bd = signal_weight_breakdown(_strong_baseline(), ZIP_MODELED_WEIGHTS_BLS)
    assert abs(bd["weight_used"] - 0.80) < 1e-9
    # All baseline rows present; no enhanced row contributes weight.
    assert all(not r["enhanced"] for r in bd["rows"] if r["present"])


def test_all_three_enhanced_signals_lift_denominator_to_100():
    feats = dict(_strong_baseline(), **_full_enhanced())
    bd = signal_weight_breakdown(feats, ZIP_MODELED_WEIGHTS_BLS)
    assert abs(bd["weight_used"] - 1.00) < 1e-9
    present_enhanced = {r["field"] for r in bd["rows"]
                        if r["present"] and r["enhanced"]}
    assert present_enhanced == set(ENHANCED_SIGNALS)


def test_any_missing_enhanced_signal_keeps_denominator_below_100():
    for missing in ENHANCED_SIGNALS:
        feats = dict(_strong_baseline(), **_full_enhanced())
        del feats[missing]
        bd = signal_weight_breakdown(feats, ZIP_MODELED_WEIGHTS_BLS)
        assert bd["weight_used"] < 1.00, f"{missing} omitted should stay < 1.00"
        assert bd["weight_used"] >= 0.80   # baseline always carries 0.80


def test_enhanced_signal_contributes_weighted_term():
    feats = dict(_strong_baseline(), **_full_enhanced())
    bd = signal_weight_breakdown(feats, ZIP_MODELED_WEIGHTS_BLS)
    row = next(r for r in bd["rows"]
               if r["field"] == "healthcare_facility_density")
    # 8.0 on bounds (0, 15) → normalized ≈ 0.533; contribution = norm * weight.
    # Both fields are independently rounded to 4 dp, so allow that slack.
    assert row["normalized"] is not None
    assert row["contribution"] > 0
    assert abs(row["contribution"] - row["normalized"] * row["weight"]) < 1e-3
