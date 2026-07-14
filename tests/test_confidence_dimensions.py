"""Tests for the three differentiation confidence dimensions:

- saturation_confidence (per-candidate; drops when demand categories
  hit Google's 20-result page-limit cap)
- catchment_overlap_confidence (cohort-level; drops when candidates
  share top-5 competitors)
- differentiation_confidence (cohort-level; drops when site_score
  coefficient of variation is tiny — math can't distinguish them)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import SCORE_WEIGHTS  # noqa: E402
from app.scoring.cohort_normalization import apply_cohort_confidence  # noqa: E402
from app.scoring.confidence_score import (  # noqa: E402
    _dimension_saturation,
    compute_confidence_score,
)


# --------------------------------------------------------------------------- #
# saturation_confidence — per-candidate
# --------------------------------------------------------------------------- #

def test_saturation_full_when_no_categories_capped():
    profile = {
        "counts_5mi": {"hospital": 3, "nursing_school": 2, "fire_station": 4},
        "saturated_demand_categories": [],
    }
    assert _dimension_saturation(profile) == 1.0


def test_saturation_drops_proportionally_when_categories_capped():
    profile = {
        "counts_5mi": {
            "hospital": 5, "nursing_school": 3, "fire_station": 2,
            "childcare_center": 20, "gym": 20,
        },
        "saturated_demand_categories": ["childcare_center", "gym"],
    }
    # 2 of 5 non-zero categories saturated → 1 - 2/5 = 0.6
    assert abs(_dimension_saturation(profile) - 0.6) < 1e-6


def test_saturation_zero_when_all_categories_capped():
    profile = {
        "counts_5mi": {"a": 20, "b": 20, "c": 20},
        "saturated_demand_categories": ["a", "b", "c"],
    }
    assert _dimension_saturation(profile) == 0.0


def test_saturation_handles_empty_counts():
    """No demand collected at all → 0 (separate from saturation but graceful)."""
    assert _dimension_saturation({"counts_5mi": {}}) == 0.0
    assert _dimension_saturation({}) == 0.0


def test_compute_confidence_score_surfaces_saturation():
    """The aggregate computer should include the new dimension."""
    profile = {
        "sources": [{"name": "US Census Bureau ACS",
                     "collected_at": "2026-05-20T00:00:00Z"}],
        "missing_fields": [],
        "counts_5mi": {"hospital": 4, "childcare_center": 20},
        "saturated_demand_categories": ["childcare_center"],
    }
    out = compute_confidence_score(profile)
    assert "saturation" in out.dimensions
    # 1 of 2 non-zero saturated → 50/100
    assert abs(out.dimensions["saturation"] - 50.0) < 0.5


# --------------------------------------------------------------------------- #
# catchment_overlap_confidence (cohort-level)
# --------------------------------------------------------------------------- #

def _candidate(name: str, site_score: float, competitor_ids: list) -> tuple:
    profile = {
        "candidate_name": name,
        "competitors": [{"place_id": pid, "distance_miles": 0.5}
                        for pid in competitor_ids],
    }
    scored = {
        "site_score": site_score,
        "sub_scores": dict.fromkeys(SCORE_WEIGHTS.keys(), 50.0),
        "confidence_breakdown": {"dimensions": {}},
    }
    return profile, scored


def test_catchment_overlap_high_confidence_when_distinct_pools():
    ranked = [
        _candidate("A", 70.0, ["p1", "p2", "p3"]),
        _candidate("B", 65.0, ["p4", "p5", "p6"]),
        _candidate("C", 60.0, ["p7", "p8", "p9"]),
    ]
    apply_cohort_confidence(ranked)
    for _, s in ranked:
        # No shared competitors → confidence should be ~100
        assert s["confidence_breakdown"]["dimensions"]["catchment_overlap"] >= 95


def test_catchment_overlap_drops_when_candidates_share_pool():
    """Three candidates share the same 5 competitors — confidence should fall."""
    shared = ["p1", "p2", "p3", "p4", "p5"]
    ranked = [
        _candidate("A", 70.0, list(shared)),
        _candidate("B", 65.0, list(shared)),
        _candidate("C", 60.0, list(shared)),
    ]
    apply_cohort_confidence(ranked)
    # All three share 100% → overlap 1.0; confidence floor = 0
    for _, s in ranked:
        assert s["confidence_breakdown"]["dimensions"]["catchment_overlap"] == 0.0


def test_catchment_overlap_partial_share():
    """Two candidates share 2 of 5 competitors; one is fully distinct."""
    ranked = [
        _candidate("A", 70.0, ["p1", "p2", "p3", "p4", "p5"]),
        _candidate("B", 65.0, ["p1", "p2", "p6", "p7", "p8"]),
        _candidate("C", 60.0, ["p9", "p10", "p11", "p12", "p13"]),
    ]
    apply_cohort_confidence(ranked)
    # B shares 2 with A; A&C and B&C are zero overlap. Confidences vary.
    dims_a = ranked[0][1]["confidence_breakdown"]["dimensions"]
    dims_c = ranked[2][1]["confidence_breakdown"]["dimensions"]
    # C should be higher than A (C shares nothing with anyone).
    assert dims_c["catchment_overlap"] > dims_a["catchment_overlap"]


# --------------------------------------------------------------------------- #
# differentiation_confidence (cohort-level)
# --------------------------------------------------------------------------- #

def test_differentiation_high_when_spread_is_wide():
    """CV ≈ 0.13 → above the 0.10 ceiling → confidence 100."""
    ranked = [
        _candidate("A", 80.0, []),
        _candidate("B", 70.0, []),
        _candidate("C", 60.0, []),
    ]
    apply_cohort_confidence(ranked)
    for _, s in ranked:
        assert s["confidence_breakdown"]["dimensions"]["differentiation"] >= 90


def test_differentiation_low_when_scores_nearly_identical():
    """CV ≈ 0.014 → below the 0.02 floor → confidence ≈ 0."""
    ranked = [
        _candidate("A", 70.0, []),
        _candidate("B", 71.0, []),
        _candidate("C", 70.5, []),
    ]
    apply_cohort_confidence(ranked)
    for _, s in ranked:
        assert s["confidence_breakdown"]["dimensions"]["differentiation"] <= 5


def test_differentiation_skipped_for_single_candidate():
    """Cohort of 1 has no spread; placeholder stays 100, no crash."""
    ranked = [_candidate("Only", 70.0, ["p1"])]
    apply_cohort_confidence(ranked)
    # Single-candidate cohort is a no-op; dimensions should remain empty.
    assert ranked[0][1]["confidence_breakdown"]["dimensions"] == {}


def test_apply_cohort_confidence_writes_both_dimensions():
    ranked = [
        _candidate("A", 70.0, ["p1"]),
        _candidate("B", 60.0, ["p2"]),
    ]
    apply_cohort_confidence(ranked)
    for _, s in ranked:
        dims = s["confidence_breakdown"]["dimensions"]
        assert "catchment_overlap" in dims
        assert "differentiation" in dims
