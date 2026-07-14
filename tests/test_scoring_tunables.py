"""Tunable scoring knobs (config-driven) actually change behavior."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_cohort_blend_is_sourced_from_config():
    import app.config as config
    from app.scoring import cohort_normalization
    assert cohort_normalization.COHORT_BLEND == config.COHORT_BLEND


def test_demand_cap_multiplier_scales_saturation(monkeypatch):
    import app.config as config
    import app.scoring.demand_score as demand_score

    # Default: 5 hospitals / cap 10 = 0.5
    base = demand_score.compute_demand_score({"hospital": 5}).by_category["hospital"]
    assert base == 0.5

    # Doubling the cap multiplier halves the normalized contribution.
    monkeypatch.setattr(config, "DEMAND_CAP_MULTIPLIER", 2.0)
    monkeypatch.setattr(demand_score, "DEMAND_CAP_MULTIPLIER", 2.0)
    scaled = demand_score.compute_demand_score({"hospital": 5}).by_category["hospital"]
    assert scaled == 0.25


def test_rent_pressure_bounds_come_from_config():
    import app.config as config
    from app.scoring import rent_estimate
    assert rent_estimate.RENT_PRESSURE_BOUNDS is config.RENT_PRESSURE_BOUNDS
    # income bound default sanity
    assert config.RENT_PRESSURE_BOUNDS["income"] == (40000.0, 160000.0)
