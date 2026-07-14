"""Tests for the Phase 3 additions:

- Candidate viability filter (#12)
- Competitor operational-scale pressure signals (#1)
- White-space opportunity formula (#8)
- Opportunity gap engine (#4)
- Confidence decomposition (#13)
- Executive decision matrix (#9)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports.interpretation import (  # noqa: E402
    build_candidate_interpretation,
    build_report_interpretation,
    decision_matrix,
)
from app.reports.opportunity_gaps import compute_opportunity_gaps  # noqa: E402
from app.scoring.competition_pressure import compute_competition_pressure  # noqa: E402
from app.scoring.confidence_score import compute_confidence_score  # noqa: E402
from app.scoring.opportunity_score import compute_opportunity_score  # noqa: E402
from app.utils.viability_filter import is_anchor_viable  # noqa: E402


# --------------------------------------------------------------------------- #
# Viability filter (#12)
# --------------------------------------------------------------------------- #

def test_viability_filter_passes_commercial_storefront():
    viable, reason = is_anchor_viable(
        types=["shopping_mall", "store"],
        name="Santana Row",
        formatted_address="377 Santana Row, San Jose, CA 95128",
    )
    assert viable is True
    assert reason == ""


def test_viability_filter_blocks_rail_yard_by_name():
    """Name pattern catches rail yards even when the place type is generic."""
    viable, reason = is_anchor_viable(
        types=["establishment"],
        name="Newark Penn Rail Yard",
        formatted_address="123 Industrial Way",
    )
    assert viable is False
    assert "rail" in reason.lower()


def test_viability_filter_blocks_rental_car_facility():
    viable, _ = is_anchor_viable(
        types=["car_rental"],
        name="SJC Rental Car Center",
    )
    assert viable is False


def test_viability_filter_blocks_data_center():
    viable, _ = is_anchor_viable(
        types=["establishment"],
        name="Equinix Silicon Valley Data Center",
    )
    assert viable is False


def test_viability_filter_blocks_fire_station_type():
    viable, reason = is_anchor_viable(
        types=["fire_station"],
        name="Engine Co. 4",
    )
    assert viable is False
    assert "non-commercial" in reason


def test_viability_filter_allows_university():
    viable, _ = is_anchor_viable(
        types=["university"],
        name="San Jose State University",
    )
    assert viable is True


# --------------------------------------------------------------------------- #
# Competition pressure (#1)
# --------------------------------------------------------------------------- #

def test_pressure_no_competitors_returns_none():
    out = compute_competition_pressure({}, demand_score_0_100=60.0)
    assert out.competition_pressure_score is None
    assert out.demand_to_competition_ratio is None
    assert out.dominant_provider_index is None


def test_pressure_high_with_large_well_reviewed_competitors():
    summary = {
        "competitor_count_total": 5,
        "competitor_total_reviews": 1200,
        "competitor_top_reviews": 700,
        "competitor_scale_large": 3,
        "competitor_scale_medium": 1,
        "competitor_scale_small": 1,
        "competitor_scale_unknown": 0,
        "competitor_low_rating_count": 0,
        "competitor_no_website": 0,
        "competitor_online_booking_missing": 0,
    }
    out = compute_competition_pressure(summary, demand_score_0_100=70.0)
    assert out.competition_pressure_score is not None
    assert out.competition_pressure_score >= 70
    assert out.dominant_provider_index is not None
    assert out.dominant_provider_index >= 0.5


def test_pressure_low_with_weak_small_competitors():
    summary = {
        "competitor_count_total": 2,
        "competitor_total_reviews": 12,
        "competitor_top_reviews": 8,
        "competitor_scale_large": 0,
        "competitor_scale_medium": 0,
        "competitor_scale_small": 2,
        "competitor_scale_unknown": 0,
        "competitor_low_rating_count": 1,
        "competitor_no_website": 2,
        "competitor_online_booking_missing": 2,
    }
    out = compute_competition_pressure(summary, demand_score_0_100=60.0)
    assert out.competition_pressure_score is not None
    assert out.competition_pressure_score < 40
    # demand >> weakened competition
    assert out.demand_to_competition_ratio is not None
    assert out.demand_to_competition_ratio >= 1.0


# --------------------------------------------------------------------------- #
# White-space opportunity (#8)
# --------------------------------------------------------------------------- #

def test_white_space_rewards_dense_high_demand_markets():
    """Crowded but huge-demand market shouldn't score worse than empty one."""
    crowded = compute_opportunity_score(
        demand_score_0_100=85.0,
        training_score_0_100=70.0,
        competition_gap_score_0_100=20.0,  # low gap (saturated)
        competition_summary={"competitor_count_total": 8},
        competition_pressure_score_0_100=70.0,
        growth_proxy=0.65,
    )
    empty = compute_opportunity_score(
        demand_score_0_100=30.0,
        training_score_0_100=20.0,
        competition_gap_score_0_100=95.0,  # huge gap (empty)
        competition_summary={"competitor_count_total": 0},
        competition_pressure_score_0_100=None,
        growth_proxy=0.65,
    )
    # Crowded high-demand market should at least not be dominated by the
    # empty low-demand one — white-space should preserve the high-demand
    # market's standing.
    assert crowded.score >= empty.score - 5
    assert crowded.white_space_score is not None


def test_white_space_none_when_no_demand():
    out = compute_opportunity_score(
        demand_score_0_100=0.0,
        training_score_0_100=0.0,
        competition_gap_score_0_100=100.0,
        competition_summary={"competitor_count_total": 0},
        competition_pressure_score_0_100=None,
        growth_proxy=0.65,
    )
    assert out.white_space_score is None


def test_white_space_score_falls_when_strong_incumbents():
    """Same demand, stronger incumbents should reduce white-space."""
    weak_pressure = compute_opportunity_score(
        demand_score_0_100=70.0,
        training_score_0_100=50.0,
        competition_gap_score_0_100=50.0,
        competition_summary={"competitor_count_total": 3},
        competition_pressure_score_0_100=20.0,
        growth_proxy=0.65,
    )
    strong_pressure = compute_opportunity_score(
        demand_score_0_100=70.0,
        training_score_0_100=50.0,
        competition_gap_score_0_100=50.0,
        competition_summary={"competitor_count_total": 3},
        competition_pressure_score_0_100=90.0,
        growth_proxy=0.65,
    )
    assert weak_pressure.white_space_score > strong_pressure.white_space_score


# --------------------------------------------------------------------------- #
# Opportunity gap engine (#4)
# --------------------------------------------------------------------------- #

def test_gap_engine_low_confidence_when_no_checks():
    out = compute_opportunity_gaps({"competitor_count_total": 5,
                                    "website_analysis_checked_count": 0})
    assert out["data_confidence"] == "low"
    assert "did not run" in out["positioning"].lower()


def test_gap_engine_detects_strong_online_booking_gap():
    summary = {
        "competitor_count_total": 6,
        "website_analysis_checked_count": 5,
        "competitor_online_booking_missing": 4,
        "competitor_class_schedule_missing": 1,
        "competitor_pricing_missing": 1,
        "competitor_acls_pals_missing": 1,
        "competitor_acls_pals_offered": 4,
        "competitor_weekend_missing": 0,
        "competitor_weekend_offered": 5,
        "competitor_group_corporate_missing": 0,
        "competitor_group_corporate_offered": 5,
        "competitor_multilingual_missing": 0,
        "competitor_multilingual_offered": 5,
        "competitor_contact_friction_detected": 0,
        "competitor_outdated_website_detected": 0,
    }
    out = compute_opportunity_gaps(summary)
    assert out["data_confidence"] == "high"
    booking_gap = next(
        g for g in out["gaps"] if g["key"] == "online_booking_gap"
    )
    assert booking_gap["strength"] == "strong"
    assert "booking" in out["positioning"].lower()


def test_gap_engine_no_strong_gaps_when_competitors_cover_basics():
    summary = {
        "competitor_count_total": 4,
        "website_analysis_checked_count": 4,
        "competitor_online_booking_missing": 0,
        "competitor_class_schedule_missing": 0,
        "competitor_pricing_missing": 0,
        "competitor_acls_pals_missing": 0,
        "competitor_acls_pals_offered": 4,
        "competitor_weekend_missing": 0,
        "competitor_weekend_offered": 4,
        "competitor_group_corporate_missing": 0,
        "competitor_group_corporate_offered": 4,
        "competitor_multilingual_missing": 0,
        "competitor_multilingual_offered": 4,
        "competitor_contact_friction_detected": 0,
        "competitor_outdated_website_detected": 0,
    }
    out = compute_opportunity_gaps(summary)
    strong = [g for g in out["gaps"] if g["strength"] == "strong"]
    assert not strong
    assert "cover the basics" in out["positioning"].lower() or \
        "differentiate" in out["positioning"].lower()


# --------------------------------------------------------------------------- #
# Confidence decomposition (#13)
# --------------------------------------------------------------------------- #

def test_confidence_dimensions_present():
    profile = {
        "sources": [
            {"name": "US Census Bureau ACS",
             "collected_at": "2026-05-20T00:00:00Z"},
            {"name": "Google Places API",
             "collected_at": "2026-05-25T00:00:00Z"},
        ],
        "missing_fields": [],
        "economy": {
            "census": {
                "data_confidence": "full",
                "values": {"population": 100000, "median_household_income": 90000},
                "indicators": {},
            },
            "real_estate": {"data_confidence": "manual_override"},
        },
        "accessibility": {
            "signals": {
                "freeway_major_road_proximity": {"status": "detected"},
                "transit_station_proximity": {"status": "not_detected"},
                "airport_business_corridor_proximity": {"status": "unknown"},
                "shopping_center_plaza_proximity": {"status": "detected"},
                "parking_proxy": {"status": "detected"},
                "walkability_proxy": {"status": "detected"},
            },
        },
        "competition_summary": {
            "competitor_count_total": 4,
            "website_analysis_checked_count": 4,
        },
        "counts_5mi": {
            "hospital": 1, "nursing_school": 1, "university": 1,
            "fire_station": 1, "childcare_center": 2,
        },
    }
    out = compute_confidence_score(profile)
    # Core six dimensions present (additional cohort dimensions like
    # saturation / catchment_overlap / differentiation are also fine).
    assert {"demographic", "accessibility", "rent",
            "competition", "demand", "data_freshness"} \
        <= set(out.dimensions.keys())
    assert out.dimensions["demographic"] >= 90  # "full" census
    assert out.dimensions["rent"] >= 90  # manual override
    assert out.dimensions["competition"] >= 90  # all checked
    assert out.dimensions["data_freshness"] > 50


def test_confidence_low_when_no_data():
    out = compute_confidence_score({})
    assert out.dimensions["demographic"] == 0.0
    assert out.dimensions["accessibility"] == 0.0
    assert out.dimensions["rent"] == 0.0


# --------------------------------------------------------------------------- #
# Executive decision matrix (#9)
# --------------------------------------------------------------------------- #

def _fake_scored(site_score: float = 70.0, tier: str = "B") -> dict:
    return {
        "site_score": site_score,
        "tier": tier,
        "tier_label": "Promising",
        "tier_reasons": [],
        "sub_scores": {
            "demand_score": 80.0,
            "healthcare_training_ecosystem_score": 60.0,
            "competition_gap_score": 40.0,
            "allcpr_opportunity_score": 70.0,
            "economy_score": 55.0,
            "accessibility_score": 65.0,
            "profitability_score": 50.0,
            "job_certification_demand_score": 0.0,
            "confidence_score": 65.0,
        },
        "competition_breakdown": {
            "effective_saturation": 0.5,
            "rationale": [],
        },
        "rent": {"rent_data_confidence": "manual_override"},
        "risks": [],
        "rationale": [],
    }


def _fake_profile(name: str = "Anchor Plaza") -> dict:
    return {
        "candidate_id": "TST-001",
        "candidate_name": name,
        "anchor": {"name": name, "formatted_address": "1 Test St",
                   "latitude": 37.0, "longitude": -121.0},
        "city": "Testville",
        "state": "CA",
        "counts_5mi": {"hospital": 2, "nursing_school": 1, "fire_station": 1},
        "competition_summary": {
            "competitor_count_total": 3,
            "competitor_count_by_bucket_mi": {5: 3},
            "competitor_avg_rating": 4.3,
            "website_analysis_checked_count": 3,
            "competitor_online_booking_missing": 2,
            "competitor_class_schedule_missing": 1,
            "competitor_pricing_missing": 1,
            "competitor_acls_pals_missing": 2,
            "competitor_acls_pals_offered": 1,
            "competitor_weekend_missing": 1,
            "competitor_weekend_offered": 2,
            "competitor_group_corporate_missing": 1,
            "competitor_group_corporate_offered": 2,
            "competitor_multilingual_missing": 2,
            "competitor_multilingual_offered": 1,
            "competitor_contact_friction_detected": 1,
            "competitor_outdated_website_detected": 0,
        },
        "accessibility": {"signals": {}},
        "economy": {
            "census": {
                "values": {"population": 50000},
                "indicators": {"working_age_share": 0.6},
            },
            "real_estate": {"data_confidence": "manual_override"},
        },
        "sources": [],
        "missing_fields": [],
    }


def test_decision_matrix_one_row_per_candidate():
    ranked = [
        (_fake_profile("Anchor Plaza"), _fake_scored(78.0, "A")),
        (_fake_profile("Edge Mall"), _fake_scored(55.0, "C")),
    ]
    rows = decision_matrix(ranked)
    assert len(rows) == 2
    assert rows[0]["candidate"] == "Anchor Plaza"
    assert rows[0]["launch_difficulty"] in ("Easy", "Medium", "Hard")
    assert "strongest_advantage" in rows[0]
    assert "biggest_risk" in rows[0]
    assert rows[0]["best_strategic_fit"]


def test_report_interpretation_includes_decision_matrix():
    ranked = [(_fake_profile(), _fake_scored())]
    out = build_report_interpretation(ranked)
    assert "decision_matrix" in out
    assert len(out["decision_matrix"]) == 1


def test_candidate_interpretation_now_includes_opportunity_gaps():
    interp = build_candidate_interpretation(_fake_profile(), _fake_scored())
    assert "opportunity_gaps" in interp
    assert "gaps" in interp["opportunity_gaps"]


def test_launch_difficulty_hard_when_dense_unknown_rent_low_confidence():
    profile = _fake_profile()
    profile["competition_summary"]["competitor_count_by_bucket_mi"] = {5: 12}
    scored = _fake_scored()
    scored["rent"]["rent_data_confidence"] = "unknown"
    scored["sub_scores"]["confidence_score"] = 30.0
    rows = decision_matrix([(profile, scored)])
    assert rows[0]["launch_difficulty"] == "Hard"
