"""Tests for the deterministic confidence penalty (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.confidence_penalty import (  # noqa: E402
    ConfidencePenalty,
    compute_confidence_penalty,
)


def test_large_sample_has_no_sample_penalty():
    p = compute_confidence_penalty(sample_size=120)
    assert isinstance(p, ConfidencePenalty)
    assert p.penalty_points == 0.0
    assert p.confidence_level == "high"


def test_sample_size_penalty_is_monotonic():
    big = compute_confidence_penalty(sample_size=120).penalty_points
    small = compute_confidence_penalty(sample_size=50).penalty_points
    moderate = compute_confidence_penalty(sample_size=20).penalty_points
    large = compute_confidence_penalty(sample_size=5).penalty_points
    assert big < small < moderate < large


def test_no_history_is_the_largest_penalty():
    no_hist = compute_confidence_penalty(has_history=False)
    tiny = compute_confidence_penalty(sample_size=5)
    assert no_hist.penalty_points > tiny.penalty_points
    assert no_hist.confidence_level in ("low", "very_low")
    assert any("histor" in r.lower() for r in no_hist.reasons)


def test_missing_fill_rate_reduces_slightly_not_destroys():
    base = compute_confidence_penalty(sample_size=120)
    with_missing = compute_confidence_penalty(
        sample_size=120, missing_fields=["fill_rate"]
    )
    assert with_missing.penalty_points > base.penalty_points
    # "slightly" — a missing fill rate must not collapse a strong sample.
    assert with_missing.penalty_points <= 5
    assert any("fill" in r.lower() for r in with_missing.reasons)


def test_missing_forecast_adds_small_penalty():
    base = compute_confidence_penalty(sample_size=120)
    with_missing = compute_confidence_penalty(
        sample_size=120, missing_fields=["forecast"]
    )
    assert with_missing.penalty_points > base.penalty_points
    assert with_missing.penalty_points <= 5


def test_stale_history_penalized_only_when_dates_exist():
    fresh = compute_confidence_penalty(sample_size=120, data_freshness_days=30)
    stale = compute_confidence_penalty(sample_size=120, data_freshness_days=900)
    no_dates = compute_confidence_penalty(sample_size=120, data_freshness_days=None)
    assert stale.penalty_points > fresh.penalty_points
    assert fresh.penalty_points == no_dates.penalty_points
    assert any("stale" in r.lower() or "old" in r.lower() for r in stale.reasons)


def test_to_dict_round_trips():
    p = compute_confidence_penalty(sample_size=20, missing_fields=["fill_rate"])
    d = p.to_dict()
    assert d["penalty_points"] == p.penalty_points
    assert d["confidence_level"] == p.confidence_level
    assert d["reasons"] == p.reasons
