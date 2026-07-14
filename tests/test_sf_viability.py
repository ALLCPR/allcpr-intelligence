"""SF-style dense-urban viability cases.

The June 2026 SF 7-mile report ranked transit stations ("Montgomery"),
intersections ("Hayes St & Divisadero St"), corporate HQs ("Pinterest"),
and random LLCs as primary candidate anchors. These tests lock in the
behavior that prevents that:

1. Hard-block transit / bus / light-rail / subway types.
2. Hard-block intersection-style ("Hayes St & Divisadero St") names.
3. Hard-block "NOT A PUBLIC STOP" markers Google sometimes emits.
4. Label viable-but-non-commercial anchors (corporate HQ, random LLC) as
   "Needs commercial site validation" instead of treating them as a
   confirmed business location.
5. Surface the large-radius warning when the configured radius is wide
   relative to a dense urban competitor footprint.
6. Compute a Δ-vs-cohort-mean badge so dense-metro candidates that all
   look numerically similar still get rank-meaningful differentiation.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports.interpretation import plain_warnings  # noqa: E402
from app.reports.markdown_report import _radius_warning_block  # noqa: E402
from app.utils.viability_filter import (  # noqa: E402
    has_commercial_signal,
    is_anchor_viable,
)


# --------------------------------------------------------------------------- #
# Hard-block: transit + intersection + "NOT A PUBLIC STOP" + parking
# --------------------------------------------------------------------------- #

def test_block_bart_subway_station_montgomery():
    viable, reason = is_anchor_viable(
        types=["subway_station", "transit_station", "point_of_interest"],
        name="Montgomery",
    )
    assert viable is False
    assert "subway_station" in reason or "transit_station" in reason


def test_block_muni_bus_station():
    viable, reason = is_anchor_viable(
        types=["bus_station", "transit_station"],
        name="Market St & 4th St Bus Stop",
    )
    assert viable is False


def test_block_light_rail_station():
    viable, reason = is_anchor_viable(
        types=["light_rail_station", "transit_station"],
        name="Embarcadero Station",
    )
    assert viable is False


def test_block_intersection_hayes_divisadero():
    viable, reason = is_anchor_viable(
        types=["route", "intersection"],
        name="Hayes St & Divisadero St",
    )
    assert viable is False
    assert "intersection" in reason.lower() or "non-commercial" in reason.lower()


def test_block_intersection_no_explicit_type():
    """Some Google results omit the route/intersection type — name pattern
    must still catch it."""
    viable, reason = is_anchor_viable(
        types=["establishment", "point_of_interest"],
        name="Polk St & Pine St",
    )
    assert viable is False
    assert "intersection" in reason.lower()


def test_block_california_pierce_intersection():
    viable, _ = is_anchor_viable(
        types=["point_of_interest"],
        name="California St & Pierce St",
    )
    assert viable is False


def test_block_not_a_public_stop_marker():
    viable, reason = is_anchor_viable(
        types=["point_of_interest"],
        name="NOT A PUBLIC STOP — Service Vehicles Only",
    )
    assert viable is False
    assert "not" in reason.lower()


def test_block_parking_lot():
    viable, _ = is_anchor_viable(
        types=["parking", "point_of_interest"],
        name="5th & Mission Parking Garage",
    )
    assert viable is False


def test_block_airport_terminal():
    viable, _ = is_anchor_viable(
        types=["airport", "point_of_interest"],
        name="San Francisco International Airport",
    )
    assert viable is False


def test_block_ambulance_bay():
    """UC Stanyan Ambulance Bay carries type=hospital but is non-public."""
    viable, reason = is_anchor_viable(
        types=["hospital", "health", "point_of_interest"],
        name="UC Stanyan Ambulance Bay",
    )
    assert viable is False
    assert "ambulance" in reason.lower()


def test_block_plus_code_reverse_geocode():
    """RJ38+HW Embarcadero, SF, CA, USA — Plus Code, not a business."""
    viable, _ = is_anchor_viable(
        types=["point_of_interest"],
        name="RJ38+HW Embarcadero, San Francisco, CA, USA",
    )
    assert viable is False


def test_block_local_government_office():
    viable, _ = is_anchor_viable(
        types=["local_government_office"],
        name="San Francisco DMV Field Office",
    )
    assert viable is False


# --------------------------------------------------------------------------- #
# Commercial signal — corporate HQs / random LLCs need site validation
# --------------------------------------------------------------------------- #

def test_corporate_hq_is_viable_but_not_commercial():
    """Pinterest HQ is a real building but not a leasable storefront."""
    viable, _ = is_anchor_viable(
        types=["establishment", "point_of_interest"],
        name="Pinterest",
    )
    assert viable is True
    is_commercial, _ = has_commercial_signal(
        types=["establishment", "point_of_interest"],
        name="Pinterest",
    )
    assert is_commercial is False


def test_random_llc_is_viable_but_not_commercial():
    viable, _ = is_anchor_viable(
        types=["establishment"],
        name="For a New Start LLC",
    )
    assert viable is True
    is_commercial, _ = has_commercial_signal(
        types=["establishment"], name="For a New Start LLC",
    )
    assert is_commercial is False


def test_random_unknown_business_is_not_commercial():
    is_commercial, _ = has_commercial_signal(
        types=["establishment", "point_of_interest"],
        name="Shahid kali",
    )
    assert is_commercial is False


def test_shopping_mall_is_commercial():
    is_commercial, reason = has_commercial_signal(
        types=["shopping_mall", "establishment"],
        name="Westfield San Francisco Centre",
    )
    assert is_commercial is True
    assert "shopping_mall" in reason


def test_medical_office_building_is_commercial_by_name():
    is_commercial, reason = has_commercial_signal(
        types=["establishment", "point_of_interest"],
        name="UCSF Mt Zion Medical Office Building",
    )
    assert is_commercial is True
    assert "medical office" in reason


def test_training_center_is_commercial_by_name():
    is_commercial, reason = has_commercial_signal(
        types=["establishment"],
        name="South Bay Training Center",
    )
    assert is_commercial is True
    assert "training" in reason


def test_coworking_is_commercial_by_name():
    is_commercial, _ = has_commercial_signal(
        types=["establishment"], name="WeWork Embarcadero",
    )
    assert is_commercial is True


# --------------------------------------------------------------------------- #
# Profile-level: "Needs commercial site validation" warning + label
# --------------------------------------------------------------------------- #

def test_warning_fires_when_anchor_needs_validation():
    profile = {
        "viability": {
            "viable": True,
            "needs_validation": True,
            "commercial_anchor": False,
        },
        "anchor": {"name": "Pinterest"},
    }
    warnings = plain_warnings({"risks": []}, profile)
    assert any("Pinterest" in w and "commercial" in w.lower()
               for w in warnings)


def test_no_validation_warning_when_anchor_is_commercial():
    profile = {
        "viability": {
            "viable": True,
            "needs_validation": False,
            "commercial_anchor": True,
        },
        "anchor": {"name": "Westfield San Francisco Centre"},
    }
    warnings = plain_warnings({"risks": []}, profile)
    assert not any("commercial storefront anchor" in w.lower()
                   for w in warnings)


# --------------------------------------------------------------------------- #
# Large-radius warning
# --------------------------------------------------------------------------- #

def _dense_profile(comp_5mi: int = 30) -> dict:
    return {
        "competition_summary": {
            "competitor_count_by_bucket_mi": {5: comp_5mi},
        }
    }


def test_radius_warning_fires_for_7mi_dense_urban():
    ranked = [(_dense_profile(30), {}), (_dense_profile(25), {})]
    block = _radius_warning_block(radius_miles=7.0, ranked=ranked)
    text = "\n".join(block)
    assert "Large radius" in text
    assert "7.0 mi" in text or "7 mi" in text


def test_radius_warning_silent_for_small_radius():
    ranked = [(_dense_profile(30), {})]
    assert _radius_warning_block(radius_miles=2.0, ranked=ranked) == []


def test_radius_warning_silent_when_no_dense_candidate():
    """Large radius is fine in suburbs / rural areas."""
    ranked = [(_dense_profile(2), {}), (_dense_profile(3), {})]
    assert _radius_warning_block(radius_miles=7.0, ranked=ranked) == []


def test_radius_warning_silent_when_no_candidates():
    assert _radius_warning_block(radius_miles=7.0, ranked=[]) == []
