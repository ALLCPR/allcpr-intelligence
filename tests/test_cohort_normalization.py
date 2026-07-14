"""Tests for cohort z-score normalization + factor decomposition.

The whole point: in a dense urban cohort where every candidate's
demand/training/competition_gap saturates, the legacy absolute scoring
collapses site_score into a 4-5 point band. Cohort normalization should
expand the spread by exposing intra-cohort differentiation.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scoring.cohort_normalization import (  # noqa: E402
    apply_cohort_normalization,
    cohort_means_from_ranked,
    factor_decomposition,
)
from app.config import SCORE_WEIGHTS  # noqa: E402


def _make_candidate(name: str, sub: dict) -> tuple:
    profile = {"candidate_id": name, "candidate_name": name}
    scored = {
        "area_score": sum(sub.get(k, 0) * w for k, w in SCORE_WEIGHTS.items()),
        "sub_scores": dict(sub),
    }
    return profile, scored


# --------------------------------------------------------------------------- #
# Score-spread expansion (the core unflatten test)
# --------------------------------------------------------------------------- #

def test_normalization_expands_spread_in_saturated_cohort():
    """Three candidates saturate demand+training but differ slightly in
    accessibility. Pre-normalization spread is tiny (driven by accessibility
    alone); post-normalization spread should be meaningfully wider.
    """
    ranked = [
        _make_candidate("A", {
            "demand_score": 95.0, "healthcare_training_ecosystem_score": 100.0,
            "competition_gap_score": 30.0, "allcpr_opportunity_score": 60.0,
            "economy_score": 69.0, "accessibility_score": 88.0,
            "profitability_score": 50.0,
        }),
        _make_candidate("B", {
            "demand_score": 92.0, "healthcare_training_ecosystem_score": 100.0,
            "competition_gap_score": 32.0, "allcpr_opportunity_score": 58.0,
            "economy_score": 69.0, "accessibility_score": 75.0,
            "profitability_score": 50.0,
        }),
        _make_candidate("C", {
            "demand_score": 88.0, "healthcare_training_ecosystem_score": 100.0,
            "competition_gap_score": 28.0, "allcpr_opportunity_score": 55.0,
            "economy_score": 69.0, "accessibility_score": 65.0,
            "profitability_score": 50.0,
        }),
    ]
    pre_spread = max(s["area_score"] for _, s in ranked) - \
        min(s["area_score"] for _, s in ranked)

    apply_cohort_normalization(ranked, blend=0.5)
    post_spread = max(s["area_score"] for _, s in ranked) - \
        min(s["area_score"] for _, s in ranked)

    assert post_spread > pre_spread, (
        f"normalization should widen spread; pre={pre_spread:.2f}, "
        f"post={post_spread:.2f}"
    )


def test_normalization_records_originals_and_adjustments():
    ranked = [
        _make_candidate("A", {
            "demand_score": 95.0, "healthcare_training_ecosystem_score": 100.0,
            "competition_gap_score": 30.0, "allcpr_opportunity_score": 60.0,
            "economy_score": 69.0, "accessibility_score": 88.0,
            "profitability_score": 50.0,
        }),
        _make_candidate("B", {
            "demand_score": 80.0, "healthcare_training_ecosystem_score": 90.0,
            "competition_gap_score": 50.0, "allcpr_opportunity_score": 55.0,
            "economy_score": 69.0, "accessibility_score": 75.0,
            "profitability_score": 50.0,
        }),
    ]
    apply_cohort_normalization(ranked, blend=0.5)
    for _, scored in ranked:
        cn = scored.get("cohort_normalization")
        assert cn["applied"] is True
        assert "demand_score" in cn["originals"]
        assert "demand_score" in cn["adjustments"]
        assert cn["cohort_size"] == 2


def test_normalization_skipped_for_single_candidate():
    """Cohort of 1 has nothing to normalize against; should be a no-op."""
    ranked = [
        _make_candidate("Only", {
            "demand_score": 50.0, "healthcare_training_ecosystem_score": 50.0,
            "competition_gap_score": 50.0, "allcpr_opportunity_score": 50.0,
            "economy_score": 50.0, "accessibility_score": 50.0,
            "profitability_score": 50.0,
        }),
    ]
    pre = ranked[0][1]["area_score"]
    apply_cohort_normalization(ranked)
    post = ranked[0][1]["area_score"]
    assert pre == post
    assert "cohort_normalization" not in ranked[0][1]


def test_zero_blend_preserves_absolute_scores():
    """blend=0.0 must leave everything unchanged (except the metadata)."""
    ranked = [
        _make_candidate("A", {
            "demand_score": 95.0, "healthcare_training_ecosystem_score": 100.0,
            "competition_gap_score": 30.0, "allcpr_opportunity_score": 60.0,
            "economy_score": 69.0, "accessibility_score": 88.0,
            "profitability_score": 50.0,
        }),
        _make_candidate("B", {
            "demand_score": 50.0, "healthcare_training_ecosystem_score": 60.0,
            "competition_gap_score": 70.0, "allcpr_opportunity_score": 40.0,
            "economy_score": 60.0, "accessibility_score": 50.0,
            "profitability_score": 50.0,
        }),
    ]
    pre_a = ranked[0][1]["sub_scores"]["demand_score"]
    pre_b = ranked[1][1]["sub_scores"]["demand_score"]
    apply_cohort_normalization(ranked, blend=0.0)
    assert ranked[0][1]["sub_scores"]["demand_score"] == pre_a
    assert ranked[1][1]["sub_scores"]["demand_score"] == pre_b


# --------------------------------------------------------------------------- #
# Cohort means
# --------------------------------------------------------------------------- #

def test_cohort_means_average_sub_scores():
    ranked = [
        _make_candidate("A", {"demand_score": 80.0, "accessibility_score": 60.0,
                               "healthcare_training_ecosystem_score": 90.0,
                               "competition_gap_score": 40.0,
                               "allcpr_opportunity_score": 50.0,
                               "economy_score": 70.0,
                               "profitability_score": 50.0}),
        _make_candidate("B", {"demand_score": 100.0, "accessibility_score": 80.0,
                               "healthcare_training_ecosystem_score": 80.0,
                               "competition_gap_score": 30.0,
                               "allcpr_opportunity_score": 60.0,
                               "economy_score": 70.0,
                               "profitability_score": 50.0}),
    ]
    means = cohort_means_from_ranked(ranked)
    assert means["demand_score"] == 90.0
    assert means["accessibility_score"] == 70.0


def test_cohort_means_empty_input():
    assert cohort_means_from_ranked([]) == {}


# --------------------------------------------------------------------------- #
# Factor decomposition
# --------------------------------------------------------------------------- #

def test_factor_decomposition_orders_by_magnitude():
    cohort_means = {
        "demand_score": 80.0,
        "healthcare_training_ecosystem_score": 80.0,
        "competition_gap_score": 30.0,
        "allcpr_opportunity_score": 50.0,
        "economy_score": 70.0,
        "accessibility_score": 70.0,
        "profitability_score": 50.0,
    }
    scored = {"sub_scores": {
        "demand_score": 85.0,  # +5, weight 0.25 → 1.25 contribution
        "healthcare_training_ecosystem_score": 80.0,  # 0
        "competition_gap_score": 25.0,  # -5, weight 0.15 → -0.75
        "allcpr_opportunity_score": 60.0,  # +10, weight 0.15 → 1.5 (largest)
        "economy_score": 70.0,
        "accessibility_score": 90.0,  # +20, weight 0.10 → 2.0 (actually largest)
        "profitability_score": 50.0,
    }}
    rows = factor_decomposition(scored, cohort_means)
    # Rows are sorted by absolute contribution descending.
    assert rows[0]["sub_score"] == "accessibility_score"
    # Sign attribution correct.
    assert rows[0]["contribution_to_site_delta"] > 0
    comp_row = next(r for r in rows if r["sub_score"] == "competition_gap_score")
    assert comp_row["contribution_to_site_delta"] < 0


def test_factor_decomposition_skips_missing_subscore():
    cohort_means = {"demand_score": 80.0, "accessibility_score": 70.0}
    scored = {"sub_scores": {"demand_score": 85.0}}  # no accessibility
    rows = factor_decomposition(scored, cohort_means)
    assert all(r["sub_score"] == "demand_score" for r in rows)


# --------------------------------------------------------------------------- #
# Degenerate-cohort guard (regression)
# --------------------------------------------------------------------------- #

def test_no_spread_cohort_is_left_unchanged():
    """A cohort with identical saturated sub-scores has nothing to normalize
    against. The scores must be left untouched — NOT dragged toward 50 by a
    fabricated unit sigma (the old _cohort_stats bug)."""
    uniform = {
        "demand_score": 80.0,
        "healthcare_training_ecosystem_score": 80.0,
        "competition_gap_score": 80.0,
        "allcpr_opportunity_score": 80.0,
    }
    ranked = [_make_candidate(f"c{i}", uniform) for i in range(3)]
    apply_cohort_normalization(ranked, blend=0.5)
    for _, scored in ranked:
        for key in uniform:
            assert scored["sub_scores"][key] == 80.0, (
                f"{key} was altered in a no-spread cohort"
            )
        # adjustments recorded as exactly zero for the explainability panel
        adj = scored["cohort_normalization"]["adjustments"]
        assert all(v == 0.0 for v in adj.values())


# --------------------------------------------------------------------------- #
# Cohort confidence: weakest-dimension rationale refresh + competitor helpers
# --------------------------------------------------------------------------- #

def test_weakest_rationale_reflects_low_catchment_overlap():
    """When candidates share their entire top-5 competitor pool, the
    catchment_overlap dimension collapses and must surface in the refreshed
    'weakest dimensions' bullet (it couldn't, before the fix)."""
    from app.scoring.cohort_normalization import apply_cohort_confidence

    shared = [{"place_id": f"p{i}", "distance_miles": i + 1.0} for i in range(5)]
    ranked = []
    for name in ("a", "b", "c"):
        profile = {"candidate_id": name, "competitors": list(shared)}
        scored = {
            "area_score": 75.0,
            "confidence_breakdown": {
                "rationale": ["weakest dimensions: rent 90/100; demand 92/100"],
                "dimensions": {"rent": 90.0, "demand": 92.0},
            },
        }
        ranked.append((profile, scored))

    apply_cohort_confidence(ranked)
    for _, scored in ranked:
        dims = scored["confidence_breakdown"]["dimensions"]
        assert dims["catchment_overlap"] == 0.0  # fully shared pool
        bullet = next(
            b for b in scored["confidence_breakdown"]["rationale"]
            if b.startswith("weakest dimensions:")
        )
        assert "catchment_overlap" in bullet  # now surfaces as weakest


def test_competitor_distance_helpers_handle_dicts_and_objects():
    from app.scoring.cohort_normalization import _comp_distance, _competitor_set_for

    class C:
        def __init__(self, pid, d):
            self.place_id = pid
            self.distance_miles = d

    assert _comp_distance({"distance_miles": 2.0}) == 2.0
    assert _comp_distance({"distance_miles": None}) == 9999.0   # missing sorts last
    assert _comp_distance(C("x", 3.5)) == 3.5
    assert _comp_distance({}) == 9999.0

    profile = {"competitors": [
        {"place_id": "far", "distance_miles": 9.0},
        C("near", 0.5),
        {"place_id": "mid", "distance_miles": 4.0},
    ]}
    # top_n=2 nearest → near (0.5) + mid (4.0), not far
    assert _competitor_set_for(profile, top_n=2) == {"near", "mid"}
