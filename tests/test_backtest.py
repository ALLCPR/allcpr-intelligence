"""Back-test analysis tests — correlation math + signal ranking."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scoring.backtest import (  # noqa: E402
    analyze_backtest,
    pearson,
    spearman,
    _rank,
)


# --------------------------------------------------------------------------- #
# Correlation math
# --------------------------------------------------------------------------- #

def test_pearson_perfect_positive():
    assert abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9


def test_pearson_perfect_negative():
    assert abs(pearson([1, 2, 3, 4], [8, 6, 4, 2]) + 1.0) < 1e-9


def test_pearson_no_spread_returns_none():
    assert pearson([5, 5, 5], [1, 2, 3]) is None


def test_pearson_too_few_points():
    assert pearson([1], [2]) is None


def test_rank_handles_ties():
    # values 10,10,20 → ranks 1.5,1.5,3
    assert _rank([10, 10, 20]) == [1.5, 1.5, 3.0]


def test_spearman_monotonic_nonlinear_is_one():
    # y = x^2 over positive x is monotonic → Spearman 1.0 even though
    # Pearson would be <1.
    xs = [1, 2, 3, 4, 5]
    ys = [1, 4, 9, 16, 25]
    assert abs(spearman(xs, ys) - 1.0) < 1e-9
    assert pearson(xs, ys) < 1.0


# --------------------------------------------------------------------------- #
# analyze_backtest
# --------------------------------------------------------------------------- #

def _row(outcome, site, demand, accessibility):
    return {
        "outcome": outcome,
        "site_score": site,
        "sub_scores": {
            "demand_score": demand,
            "accessibility_score": accessibility,
        },
    }


def test_analyze_ranks_best_predictor_first():
    # demand tracks outcome perfectly; accessibility is noise.
    rows = [
        _row(100, 60, 90, 30),
        _row(200, 65, 95, 80),
        _row(50, 55, 70, 50),
        _row(150, 62, 88, 20),
    ]
    report = analyze_backtest(rows, outcome_name="enrollment")
    assert report.n == 4
    best = report.best_predictor()
    assert best is not None
    # demand_score should out-predict accessibility_score
    demand = next(c for c in report.correlations if c.signal == "demand_score")
    access = next(c for c in report.correlations
                  if c.signal == "accessibility_score")
    assert abs(demand.spearman) >= abs(access.spearman)


def test_analyze_flags_small_sample():
    rows = [_row(100, 60, 90, 30), _row(200, 65, 95, 80)]
    report = analyze_backtest(rows)
    assert any("usable rows" in n for n in report.notes)


def test_analyze_skips_non_numeric_outcome():
    rows = [
        _row(100, 60, 90, 30),
        {"outcome": "n/a", "site_score": 70, "sub_scores": {}},
        _row(200, 65, 95, 80),
        _row(150, 62, 88, 20),
    ]
    report = analyze_backtest(rows)
    assert report.n == 3  # the "n/a" row dropped


def test_signal_strength_buckets():
    rows = [
        _row(10, 10, 10, 50),
        _row(20, 20, 20, 50),
        _row(30, 30, 30, 50),
        _row(40, 40, 40, 50),
    ]
    report = analyze_backtest(rows)
    site = next(c for c in report.correlations if c.signal == "site_score")
    assert site.strength == "strong"
    assert site.direction == "positive"
    # accessibility is constant → no spread → correlation undefined, skipped
    access = [c for c in report.correlations
              if c.signal == "accessibility_score"]
    assert access == [] or access[0].pearson is None
