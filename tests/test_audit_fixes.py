"""Tests for the audit-pass fixes:

- Saturated Google Places count rendering (≥20)
- Competitor website fetch caching
- Location descriptor prioritization (healthcare/education over airport)
- Integration-test mock-path defense
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.website_analysis import (  # noqa: E402
    WEBSITE_ANALYSIS_TTL_SECONDS,
    analyze_website,
)
from app.reports.interpretation import (  # noqa: E402
    _location_descriptor,
    demand_signals_ranked,
)
from app.reports.markdown_report import _demand_signals_table  # noqa: E402
from app.utils.cache import Cache  # noqa: E402


# --------------------------------------------------------------------------- #
# Saturated count rendering
# --------------------------------------------------------------------------- #

def test_saturated_category_renders_with_geq_prefix():
    profile = {
        "counts_5mi": {"childcare_center": 20, "hospital": 4},
        "saturated_demand_categories": ["childcare_center"],
    }
    ranked = demand_signals_ranked(profile)
    by_key = {
        r["key"]: r
        for r in ranked["high_value"] + ranked["secondary"]
    }
    assert by_key["childcare_center"]["count_display"] == "≥20"
    assert by_key["childcare_center"]["saturated"] is True
    assert by_key["hospital"]["count_display"] == "4"
    assert by_key["hospital"]["saturated"] is False


def test_demand_signals_table_uses_count_display():
    rows = [
        {
            "signal": "childcare center",
            "key": "childcare_center",
            "count": 20,
            "count_display": "≥20",
            "importance": "Medium",
            "why": "State law mandates CPR for childcare staff.",
        }
    ]
    rendered = "\n".join(_demand_signals_table(rows))
    assert "≥20" in rendered


def test_demand_signals_table_falls_back_to_count_when_no_display():
    """Back-compat: rows without count_display still render the raw count."""
    rows = [
        {
            "signal": "hospital",
            "key": "hospital",
            "count": 3,
            "importance": "Very high",
            "why": "Hospitals drive recurring staff BLS.",
        }
    ]
    rendered = "\n".join(_demand_signals_table(rows))
    assert "| 3 |" in rendered


# --------------------------------------------------------------------------- #
# Website fetch caching
# --------------------------------------------------------------------------- #

def test_analyze_website_uses_cache_on_second_call(tmp_path):
    """A second call with cache should reuse the first result, not re-fetch."""
    cache = Cache(tmp_path / "test_cache.sqlite", mode="auto")

    call_counter = {"n": 0}

    def fake_live(normalized, *, session, timeout):
        call_counter["n"] += 1
        return {
            "checked": True,
            "detected": ["online_booking"],
            "missing": [],
            "unknown": [],
            "pages_checked": ["https://example.com"],
            "retrieved_at": "2026-05-01T00:00:00Z",
            "error": "",
        }

    with patch("app.collectors.website_analysis._analyze_live",
               side_effect=fake_live):
        first = analyze_website("https://acme-cpr.example.com",
                                cache=cache)
        second = analyze_website("https://acme-cpr.example.com",
                                 cache=cache)

    assert call_counter["n"] == 1, "second call should be a cache hit"
    assert first["checked"] == second["checked"]
    assert first["detected"] == second["detected"]


def test_analyze_website_no_cache_always_calls_live(tmp_path):
    """Passing cache=None bypasses the cache (production fallback path)."""
    call_counter = {"n": 0}

    def fake_live(normalized, *, session, timeout):
        call_counter["n"] += 1
        return {
            "checked": True, "detected": [], "missing": [],
            "unknown": [], "pages_checked": [],
            "retrieved_at": "2026-05-01T00:00:00Z", "error": "",
        }

    with patch("app.collectors.website_analysis._analyze_live",
               side_effect=fake_live):
        analyze_website("https://acme-cpr.example.com", cache=None)
        analyze_website("https://acme-cpr.example.com", cache=None)

    assert call_counter["n"] == 2


def test_ttl_constant_sane():
    # 14 days; if someone bumps to zero/negative the cache becomes a no-op.
    assert WEBSITE_ANALYSIS_TTL_SECONDS >= 86400


# --------------------------------------------------------------------------- #
# Location descriptor prioritization
# --------------------------------------------------------------------------- #

def _profile_with(
    counts: dict,
    airport_distance: float | None = None,
) -> dict:
    profile = {"counts_5mi": counts}
    if airport_distance is not None:
        profile["accessibility"] = {
            "signals": {
                "airport_business_corridor_proximity": {
                    "status": "detected",
                    "distance_miles": airport_distance,
                }
            }
        }
    return profile


def test_descriptor_education_corridor_wins_over_airport():
    """A medical-school-heavy area near an airport reads as education, not airport."""
    profile = _profile_with(
        counts={
            "nursing_school": 4, "medical_school": 8,
            "community_college": 3, "university": 2,
            "hospital": 2,
        },
        airport_distance=0.8,
    )
    assert "Education corridor" in _location_descriptor(profile)


def test_descriptor_dense_healthcare_wins_over_airport():
    profile = _profile_with(
        counts={"hospital": 5, "urgent_care": 3, "medical_clinic": 4},
        airport_distance=0.8,
    )
    assert "Healthcare-adjacent" in _location_descriptor(profile)


def test_descriptor_airport_kept_when_no_strong_other_signals():
    profile = _profile_with(
        counts={"gym": 2, "physical_therapy": 1},
        airport_distance=0.8,
    )
    assert "Airport" in _location_descriptor(profile)


def test_descriptor_thin_data_falls_back_to_mixed_use():
    profile = _profile_with(counts={"gym": 1})
    assert "Mixed-use" in _location_descriptor(profile)
