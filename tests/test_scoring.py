"""
Scoring-layer tests.

These tests verify the *math* of the scoring functions, not real-world
accuracy. They use synthetic profiles so they run with no external APIs.
"""
from __future__ import annotations

from app.config import SCORE_WEIGHTS
from app.scoring.competition_score import compute_competition_gap_score
from app.scoring.confidence_score import compute_confidence_score
from app.scoring.demand_score import (
    compute_demand_score,
    compute_training_ecosystem_score,
)
from app.scoring.economy_score import (
    compute_accessibility_score,
    compute_economy_score,
)
from app.scoring.opportunity_score import compute_opportunity_score
from app.scoring.profitability import estimate_profitability
from app.scoring.recommendation_tier import compute_tier
from app.scoring.site_score import score_profile
from app.utils.source_tracker import utcnow_iso


# ----- demand ----------------------------------------------------------------

def test_demand_score_zero_inputs_is_zero():
    b = compute_demand_score({})
    assert b.score == 0.0
    assert b.rationale == []


def test_demand_score_increases_monotonically_with_hospitals():
    low = compute_demand_score({"hospital": 0}).score
    mid = compute_demand_score({"hospital": 2}).score
    high = compute_demand_score({"hospital": 8}).score
    assert low < mid <= high


def test_demand_score_caps_at_100():
    # Stuff every category beyond its cap.
    counts = {
        "hospital": 50, "urgent_care": 50, "fire_station": 50, "ems": 50,
        "nursing_school": 50, "medical_school": 50, "dental_school": 50,
        "community_college": 50, "university": 50, "childcare_center": 200,
        "senior_care": 50, "gym": 200, "physical_therapy": 200,
        "dental_clinic": 200, "medical_clinic": 200, "emt_training": 50,
        "cna_training": 50, "healthcare_training": 50,
    }
    assert compute_demand_score(counts).score == 100.0


def test_training_ecosystem_score_uses_only_training_keys():
    # Hospitals and gyms should NOT affect training-ecosystem score.
    s1 = compute_training_ecosystem_score({"hospital": 10, "gym": 100}).score
    s2 = compute_training_ecosystem_score({"nursing_school": 3}).score
    assert s1 == 0.0
    assert s2 > 0.0


# ----- competition -----------------------------------------------------------

def test_competition_gap_high_when_demand_high_and_no_competitors():
    summary = {
        "competitor_count_by_bucket_mi": {1: 0, 3: 0, 5: 0, 10: 0},
        "competitor_avg_rating": None,
        "competitor_total_reviews": 0,
        "competitor_no_website": 0,
    }
    b = compute_competition_gap_score(demand_score_0_100=90, competition_summary=summary)
    assert b.score >= 80  # sqrt(0.9 * 1.0) ~= 0.95


def test_competition_gap_low_when_saturated():
    summary = {
        "competitor_count_by_bucket_mi": {1: 4, 3: 8, 5: 10, 10: 15},
        "competitor_avg_rating": 4.7,
        "competitor_total_reviews": 1500,
        "competitor_no_website": 0,
    }
    b = compute_competition_gap_score(demand_score_0_100=80, competition_summary=summary)
    assert b.score < 30


def test_competition_weak_competitors_open_gap():
    # Same count, but weak competitors should give a higher gap score.
    base_summary = {
        "competitor_count_by_bucket_mi": {1: 1, 3: 2, 5: 3, 10: 4},
        "competitor_total_reviews": 10,
        "competitor_no_website": 2,
    }
    strong = compute_competition_gap_score(
        80, {**base_summary, "competitor_avg_rating": 4.8}).score
    weak = compute_competition_gap_score(
        80, {**base_summary, "competitor_avg_rating": 3.2}).score
    assert weak > strong


# ----- economy ---------------------------------------------------------------

def test_economy_score_uses_neutral_default_when_no_data():
    """Missing Census data must NOT deflate site_score to 0.

    We return a neutral score (50 by default) and flag data_confidence='missing'
    so the report can warn. Confidence_score still penalizes missing fields.
    """
    b = compute_economy_score({"census": {"values": {}, "indicators": {}}})
    assert b.score == 50.0
    assert b.used_fields == []
    assert b.data_confidence == "missing"


def test_economy_score_full_data_marks_confidence_ok():
    block = {"census": {
        "values": {
            "median_household_income": 80_000,
            "population": 100_000,
        },
        "indicators": {
            "healthcare_employment_share": 0.13,
            "working_age_share": 0.65,
            "bachelors_or_higher_share": 0.30,
            "employment_rate": 0.62,
        },
    }}
    b = compute_economy_score(block)
    assert b.data_confidence == "ok"


def test_economy_score_partial_data_marks_confidence_partial():
    b = compute_economy_score({"census": {
        "values": {"median_household_income": 60_000},
        "indicators": {},
    }})
    assert b.data_confidence == "partial"


def test_economy_score_partial_data_does_not_deflate():
    """When only some fields are present we rescale by used weight."""
    block = {
        "census": {
            "values": {
                "median_household_income": 110_000,
                "population": 200_000,
            },
            "indicators": {},
        }
    }
    b = compute_economy_score(block)
    # Both fields at their high end => normalized 1.0 each => score 100.
    assert b.score == 100.0
    assert set(b.used_fields) == {"median_household_income", "population"}


def test_economy_score_missing_fields_listed():
    b = compute_economy_score({"census": {
        "values": {"median_household_income": 60_000},
        "indicators": {},
    }})
    assert "healthcare_employment_share" in b.missing_fields
    assert "median_household_income" in b.used_fields


# ----- accessibility ---------------------------------------------------------

def test_accessibility_proxy_from_1mi_counts():
    counts_by_bucket = {
        "hospital": {1: 2, 3: 4, 5: 6, 10: 8},
        "gym": {1: 5, 3: 8, 5: 12, 10: 20},
    }
    assert compute_accessibility_score(counts_by_bucket) > 0.0


def test_accessibility_zero_when_empty():
    assert compute_accessibility_score({}) == 0.0


# ----- confidence ------------------------------------------------------------

def test_confidence_zero_when_no_sources():
    b = compute_confidence_score({"sources": [], "missing_fields": []})
    assert b.score == 0.0


def test_confidence_stub_sources_do_not_count():
    profile = {
        "sources": [{
            "name": "BLS / labor market (not yet integrated)",
            "url": "", "collected_at": utcnow_iso(),
            "fields": [], "notes": "stub",
        }],
        "missing_fields": [],
    }
    assert compute_confidence_score(profile).score == 0.0


def test_confidence_increases_with_credible_sources():
    fresh = utcnow_iso()
    profile = {
        "sources": [
            {"name": "Google Places API (Nearby Search)", "url": "x",
             "fields": [], "collected_at": fresh},
            {"name": "US Census Bureau ACS 5-year (2022)", "url": "y",
             "fields": [], "collected_at": fresh},
        ],
        "missing_fields": [],
    }
    assert compute_confidence_score(profile).score > 40


def test_confidence_missing_fields_penalize():
    fresh = utcnow_iso()
    sources = [{"name": "Google Places API", "url": "x",
                "fields": [], "collected_at": fresh}]
    full = compute_confidence_score({"sources": sources, "missing_fields": []}).score
    sparse = compute_confidence_score({
        "sources": sources,
        "missing_fields": [f"missing_{i}" for i in range(10)],
    }).score
    assert sparse < full


def test_confidence_unrecognized_source_does_not_dilute_quality():
    """A generic 'competitor website fetch' (unrated) must not drag the
    quality average down. Unrecognized sources contribute nothing."""
    fresh = utcnow_iso()
    rated_only = [
        {"name": "US Census Bureau ACS", "url": "c",
         "fields": [], "collected_at": fresh},
    ]
    with_noise = rated_only + [
        {"name": "Competitor website homepage fetch", "url": "h",
         "fields": [], "collected_at": fresh},
    ]
    score_rated = compute_confidence_score(
        {"sources": rated_only, "missing_fields": []}).score
    score_with_noise = compute_confidence_score(
        {"sources": with_noise, "missing_fields": []}).score
    # Adding an unrecognized source must NOT lower the score.
    assert score_with_noise >= score_rated


# ----- site_score integration ------------------------------------------------

def _synthetic_profile(**overrides) -> dict:
    profile = {
        "candidate_id": "TEST-001",
        "city": "Testville", "state": "CA",
        "latitude": 34.0, "longitude": -118.0,
        "counts_5mi": {"hospital": 2, "fire_station": 2, "nursing_school": 1},
        "counts_by_bucket": {
            "hospital": {1: 1, 3: 2, 5: 2, 10: 3},
            "fire_station": {1: 1, 3: 2, 5: 2, 10: 3},
            "nursing_school": {1: 0, 3: 1, 5: 1, 10: 2},
        },
        "competition_summary": {
            "competitor_count_by_bucket_mi": {1: 0, 3: 1, 5: 2, 10: 3},
            "competitor_avg_rating": 4.2,
            "competitor_total_reviews": 80,
            "competitor_no_website": 0,
        },
        "economy": {"census": {
            "values": {
                "population": 75_000,
                "median_household_income": 78_000,
                "median_age": 38,
            },
            "indicators": {
                "healthcare_employment_share": 0.13,
                "working_age_share": 0.7,
                "bachelors_or_higher_share": 0.35,
                "employment_rate": 0.65,
            },
        }, "labor": {"values": {}, "indicators": {}},
            "real_estate": {"values": {}, "indicators": {}}},
        "sources": [
            {"name": "Google Places API", "url": "g",
             "fields": [], "collected_at": utcnow_iso()},
            {"name": "US Census Bureau ACS 5-year (2022)", "url": "c",
             "fields": [], "collected_at": utcnow_iso()},
        ],
        "missing_fields": [],
    }
    profile.update(overrides)
    return profile


def test_site_score_combines_sub_scores_per_weights():
    profile = _synthetic_profile()
    result = score_profile(profile)
    sub = result["sub_scores"]
    expected = sum(sub[k] * w for k, w in SCORE_WEIGHTS.items())
    # The weighted blend is now the AREA score; site_score is gated separately.
    assert abs(result["area_score"] - round(expected, 2)) < 0.05
    assert 0 <= result["area_score"] <= 100


def test_site_score_rationale_is_explainable():
    """Every recommendation must come with rationale lines (no silent black box)."""
    profile = _synthetic_profile()
    result = score_profile(profile)
    assert result["rationale"], "site_score must produce rationale bullets"
    assert any("Demand drivers" in r for r in result["rationale"])


# ----- opportunity score ----------------------------------------------------

def test_opportunity_low_when_everything_is_weak():
    summary = {
        "competitor_count_total": 0,
        "competitor_no_website": 0,
        "competitor_no_phone": 0,
        "competitor_low_rating_count": 0,
    }
    b = compute_opportunity_score(
        demand_score_0_100=10, training_score_0_100=5,
        competition_gap_score_0_100=20, competition_summary=summary,
    )
    assert b.score < 40
    assert b.angles, "should always suggest at least one angle"


def test_opportunity_boosted_by_competitor_weakness():
    summary_strong = {
        "competitor_count_total": 10,
        "competitor_no_website": 0,
        "competitor_no_phone": 0,
        "competitor_low_rating_count": 0,
    }
    summary_weak = {
        "competitor_count_total": 10,
        "competitor_no_website": 6,
        "competitor_no_phone": 5,
        "competitor_low_rating_count": 4,
    }
    strong = compute_opportunity_score(80, 60, 50, summary_strong).score
    weak = compute_opportunity_score(80, 60, 50, summary_weak).score
    assert weak > strong


def test_opportunity_angles_reflect_nearby_signals():
    breakdown_dem = {"hospital": 0.8, "childcare_center": 0.6}
    breakdown_train = {"nursing_school": 0.9}
    b = compute_opportunity_score(
        80, 80, 60,
        competition_summary={
            "competitor_count_total": 5,
            "competitor_no_website": 0,
            "competitor_no_phone": 0,
            "competitor_low_rating_count": 0,
        },
        demand_breakdown=breakdown_dem,
        training_breakdown=breakdown_train,
    )
    angles_text = " | ".join(b.angles).lower()
    assert "nursing" in angles_text
    assert "hospital" in angles_text or "bls" in angles_text


def test_opportunity_boosted_by_job_certification_demand():
    summary = {
        "competitor_count_total": 4,
        "competitor_no_website": 0,
        "competitor_no_phone": 0,
        "competitor_low_rating_count": 0,
    }
    without_jobs = compute_opportunity_score(60, 50, 50, summary).score
    with_jobs = compute_opportunity_score(
        60, 50, 50, summary, job_demand_score_0_100=80,
    )
    assert with_jobs.score > without_jobs
    assert any("job postings" in r for r in with_jobs.rationale)


# ----- profitability --------------------------------------------------------

def test_profitability_marked_estimated():
    p = estimate_profitability(60, 60, 60)
    assert p.confidence == "estimated"
    assert any("estimated" in n.lower() or "estimate" in n.lower() for n in p.notes)


def test_profitability_scales_with_opportunity():
    low = estimate_profitability(10, 10, 10)
    high = estimate_profitability(90, 90, 90)
    assert high.students_mid > low.students_mid
    assert high.revenue_mid > low.revenue_mid
    assert high.score > low.score


def test_profitability_band_is_ordered_low_mid_high():
    p = estimate_profitability(60, 60, 60)
    assert p.students_low <= p.students_mid <= p.students_high
    assert p.revenue_low <= p.revenue_mid <= p.revenue_high


# ----- recommendation tier --------------------------------------------------

def test_tier_high_score_high_confidence_is_A():
    # A requires a VALIDATED commercial space; without it, high area_score
    # caps at B (area-level only).
    v = compute_tier(site_score=85, confidence_score=70,
                     effective_saturation=0.3, competition_gap_score=70,
                     site_validated=True)
    assert v.tier == "A"
    assert v.executive_state == "Lease-ready candidate"


def test_tier_unvalidated_high_score_caps_at_B():
    v = compute_tier(site_score=85, confidence_score=70,
                     effective_saturation=0.3, competition_gap_score=70,
                     candidate_type="commercial_area_proxy", site_validated=False)
    assert v.tier == "B"
    assert v.executive_state == "Recommended for listing search"


def test_tier_low_confidence_caps_at_C():
    v = compute_tier(site_score=85, confidence_score=20,
                     effective_saturation=0.3, competition_gap_score=70)
    assert v.tier == "C"
    assert any("confidence" in r.lower() for r in v.reasons)


def test_tier_saturated_market_caps_at_C():
    v = compute_tier(site_score=85, confidence_score=70,
                     effective_saturation=0.95, competition_gap_score=10)
    assert v.tier == "C"


def test_tier_low_score_is_F():
    v = compute_tier(site_score=20, confidence_score=70,
                     effective_saturation=0.3, competition_gap_score=70)
    assert v.tier == "F"


# ----- site_score with new sub-scores ----------------------------------------

def test_site_score_includes_new_sub_scores():
    profile = _synthetic_profile()
    result = score_profile(profile)
    sub = result["sub_scores"]
    for k in ("allcpr_opportunity_score", "profitability_score"):
        assert k in sub
        assert 0 <= sub[k] <= 100
    assert "tier" in result
    assert result["tier"] in ("A", "B", "C", "D", "F")
    assert "profitability_estimate" in result
    assert result["profitability_estimate"]["confidence"] == "estimated"


def test_site_score_with_all_missing_data_yields_low_confidence_flag():
    profile = _synthetic_profile(
        counts_5mi={}, counts_by_bucket={},
        competition_summary={"competitor_count_by_bucket_mi": {}},
        economy={"census": {"values": {}, "indicators": {}},
                 "labor": {"values": {}, "indicators": {}},
                 "real_estate": {"values": {}, "indicators": {}}},
        sources=[],
        missing_fields=[f"x_{i}" for i in range(15)],
    )
    result = score_profile(profile)
    assert result["sub_scores"]["confidence_score"] < 50
    assert any("confidence" in r.lower() for r in result["risks"])
