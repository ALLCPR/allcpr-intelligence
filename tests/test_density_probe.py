"""Tests for dense-metro auto-detection.

The probe should flip into dense-mode when 20+ CPR competitors exist
within the configured radius and recommend a tighter radius + grid.
Suburban probes (1-5 results) should leave the configured values intact.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports.markdown_report import _dense_mode_block  # noqa: E402
from app.utils.density_probe import (  # noqa: E402
    DENSE_GRID_SPACING_MILES,
    DENSE_RADIUS_MILES,
    probe_density,
)


def _fake_client(result_count: int) -> MagicMock:
    """Return a client whose text_search returns ``result_count`` fake places."""
    client = MagicMock()
    client.text_search.return_value = [
        {"place_id": f"pid_{i}", "name": f"CPR Provider {i}"}
        for i in range(result_count)
    ]
    return client


def test_dense_metro_triggers_rescale():
    client = _fake_client(35)
    probe = probe_density(
        client, center=(37.7749, -122.4194),
        radius_miles=7.0, grid_spacing_miles=2.0,
    )
    assert probe.is_dense is True
    assert probe.competitor_count == 35
    assert probe.recommended_radius_miles == DENSE_RADIUS_MILES
    assert probe.recommended_grid_spacing_miles == DENSE_GRID_SPACING_MILES
    assert "dense-metro mode" in probe.reason


def test_suburban_keeps_configured_values():
    client = _fake_client(3)
    probe = probe_density(
        client, center=(36.5, -119.6),  # rural Central Valley
        radius_miles=5.0, grid_spacing_miles=2.5,
    )
    assert probe.is_dense is False
    assert probe.competitor_count == 3
    assert probe.recommended_radius_miles == 5.0
    assert probe.recommended_grid_spacing_miles == 2.5
    assert "keeping configured" in probe.reason


def test_boundary_at_threshold():
    """Exactly the threshold should trigger dense mode."""
    client = _fake_client(20)
    probe = probe_density(
        client, center=(0, 0), radius_miles=5.0, grid_spacing_miles=2.0,
        threshold=20,
    )
    assert probe.is_dense is True


def test_dense_mode_doesnt_inflate_already_tight_radius():
    """If the user already configured radius=1, dense-mode shouldn't enlarge it."""
    client = _fake_client(50)
    probe = probe_density(
        client, center=(0, 0), radius_miles=1.0, grid_spacing_miles=0.4,
    )
    assert probe.is_dense is True
    assert probe.recommended_radius_miles == 1.0  # not raised to DENSE_RADIUS_MILES
    assert probe.recommended_grid_spacing_miles == 0.4


def test_probe_failure_returns_safe_default():
    """A Places error must not crash the pipeline — return is_dense=False."""
    client = MagicMock()
    client.text_search.side_effect = RuntimeError("boom")
    probe = probe_density(
        client, center=(0, 0), radius_miles=5.0, grid_spacing_miles=2.0,
    )
    assert probe.is_dense is False
    assert probe.competitor_count == 0
    assert "probe failed" in probe.reason


def test_probe_empty_results():
    client = _fake_client(0)
    probe = probe_density(
        client, center=(0, 0), radius_miles=5.0, grid_spacing_miles=2.0,
    )
    assert probe.is_dense is False
    assert probe.competitor_count == 0


# --------------------------------------------------------------------------- #
# Report banner
# --------------------------------------------------------------------------- #

def test_dense_mode_banner_renders_when_active():
    profile = {
        "density_probe": {
            "is_dense": True,
            "competitor_count": 35,
            "configured_radius_miles": 7.0,
            "effective_radius_miles": 2.0,
            "effective_grid_spacing_miles": 0.6,
            "reason": "test",
        }
    }
    out = _dense_mode_block([(profile, {})])
    text = "\n".join(out)
    assert "Dense-metro mode" in text
    assert "35 CPR" in text
    assert "7.0-mile" in text
    assert "2.0 mi" in text
    assert "0.6 mi" in text


def test_dense_mode_banner_silent_when_not_dense():
    profile = {
        "density_probe": {
            "is_dense": False,
            "competitor_count": 3,
        }
    }
    out = _dense_mode_block([(profile, {})])
    assert out == []


def test_dense_mode_banner_silent_when_no_probe():
    """Legacy behavior — when probe wasn't run, no banner."""
    out = _dense_mode_block([({"density_probe": None}, {})])
    assert out == []
