"""Tests for Tier 2 integrations:

- OSM commercial-zone filtering of candidate grid points
- Foursquare adapter response → PlaceProfile shape (with mocked HTTP)
- Mapbox isochrone polygon → catchment-filter post-pass
- Feature-flag fallbacks: pipeline behaves unchanged when keys are absent
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import foursquare_places, mapbox_isochrones, osm_zoning  # noqa: E402
from app.utils.catchment_filter import apply_catchment_filter  # noqa: E402
from app.utils.commercial_zones import (  # noqa: E402
    bbox_for_radius,
    distance_to_polygon_meters,
    filter_points_to_commercial,
    haversine_meters,
    point_in_polygon,
    point_within_any_polygon,
)


# --------------------------------------------------------------------------- #
# Commercial zone polygon helpers
# --------------------------------------------------------------------------- #

def test_point_in_polygon_simple_square():
    square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    assert point_in_polygon((0.5, 0.5), square) is True
    assert point_in_polygon((2.0, 2.0), square) is False


def test_point_in_polygon_concave():
    """L-shape: point in the notch should not be inside."""
    L = [(0.0, 0.0), (0.0, 2.0), (1.0, 2.0), (1.0, 1.0),
         (2.0, 1.0), (2.0, 0.0)]
    assert point_in_polygon((0.5, 0.5), L) is True
    assert point_in_polygon((1.5, 1.5), L) is False  # in the notch


def test_haversine_meters_known_distance():
    # SF City Hall ↔ Ferry Building ≈ 2.9 km (great-circle).
    sf_city_hall = (37.7794, -122.4193)
    ferry = (37.7955, -122.3937)
    d = haversine_meters(sf_city_hall, ferry)
    assert 2700 < d < 3100


def test_filter_points_keeps_inside_or_near():
    poly = [(0.0, 0.0), (0.0, 0.001), (0.001, 0.001), (0.001, 0.0)]
    inside = (0.0005, 0.0005)
    far = (10.0, 10.0)
    kept, dropped = filter_points_to_commercial(
        [inside, far], [poly], max_distance_meters=0.0, min_keep=1,
    )
    assert inside in kept
    assert far in dropped


def test_filter_points_falls_back_when_no_match():
    """min_keep guarantees we don't return empty when zoning is sparse."""
    poly = [(0.0, 0.0), (0.0, 0.001), (0.001, 0.001), (0.001, 0.0)]
    points = [(10.0, 10.0), (11.0, 11.0)]
    kept, dropped = filter_points_to_commercial(
        points, [poly], max_distance_meters=10.0, min_keep=3,
    )
    # Both points kept (filter falls back) and nothing reported as dropped.
    assert set(kept) == set(points)
    assert dropped == []


def test_filter_points_empty_polygons_keeps_all():
    points = [(0.0, 0.0), (1.0, 1.0)]
    kept, dropped = filter_points_to_commercial(points, [], min_keep=1)
    assert kept == points and dropped == []


def test_bbox_for_radius_shape():
    s, w, n, e = bbox_for_radius((37.7749, -122.4194), 5.0)
    # All four sides must straddle the center coordinate.
    assert s < 37.7749 < n
    assert w < -122.4194 < e
    # 5mi north-south is ~0.072° of latitude; 5/69 = ~0.0725.
    assert abs((n - s) / 2 - 5.0 / 69.0) < 0.01


# --------------------------------------------------------------------------- #
# OSM zoning collector (feature-flag fallback)
# --------------------------------------------------------------------------- #

def test_osm_zoning_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(osm_zoning, "OSM_ZONING_ENABLED", False)
    polys = osm_zoning.fetch_commercial_polygons((37.7, -122.5, 37.8, -122.4))
    assert polys == []


def test_osm_zoning_invalid_bbox_returns_empty():
    # south > north — invalid bbox
    polys = osm_zoning.fetch_commercial_polygons((38.0, -122.0, 37.0, -121.0))
    assert polys == []


# --------------------------------------------------------------------------- #
# Foursquare adapter (mocked HTTP)
# --------------------------------------------------------------------------- #

def test_foursquare_not_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(foursquare_places, "FOURSQUARE_API_KEY", "")
    out = foursquare_places.search_commercial_anchors(
        origin=(37.7749, -122.4194), radius_miles=1.0,
    )
    assert out == []


def test_foursquare_response_maps_to_placeprofile(monkeypatch):
    monkeypatch.setattr(
        foursquare_places, "FOURSQUARE_API_KEY", "test-key",
    )
    fake_response = [
        {
            "fsq_place_id": "abc123",
            "name": "Westfield SF Centre",
            "categories": [
                {"fsq_category_id": "4bf58dd8d48988d1fd941735", "name": "Shopping Mall"},
            ],
            "latitude": 37.7838,
            "longitude": -122.4079,
            "distance": 320,
            "location": {
                "address": "865 Market St", "locality": "San Francisco",
                "region": "CA", "postcode": "94103",
            },
        },
    ]
    with patch.object(foursquare_places, "_live_search",
                      return_value=fake_response):
        out = foursquare_places.search_commercial_anchors(
            origin=(37.7794, -122.4193), radius_miles=1.0,
        )
    assert len(out) == 1
    profile = out[0]
    assert profile.name == "Westfield SF Centre"
    assert profile.place_id == "fsq:abc123"
    assert "Shopping Mall" in profile.types
    assert profile.distance_miles is not None
    assert profile.source_api.startswith("Foursquare")


# --------------------------------------------------------------------------- #
# Mapbox isochrones (feature-flag + filter)
# --------------------------------------------------------------------------- #

def test_mapbox_not_configured_returns_none(monkeypatch):
    monkeypatch.setattr(mapbox_isochrones, "MAPBOX_TOKEN", "")
    poly = mapbox_isochrones.fetch_isochrone((37.7749, -122.4194), minutes=10)
    assert poly is None


def test_apply_catchment_filter_drops_outside_competitors():
    # Tiny polygon around the origin.
    polygon = [
        (37.7740, -122.4200), (37.7740, -122.4180),
        (37.7760, -122.4180), (37.7760, -122.4200),
    ]
    profile = {
        "demand_top_places": {
            "hospital": [
                {"latitude": 37.7750, "longitude": -122.4190,
                 "distance_miles": 0.1},  # inside
                {"latitude": 37.8000, "longitude": -122.4000,
                 "distance_miles": 5.0},  # outside
            ],
        },
        "competitors": [
            {"latitude": 37.7750, "longitude": -122.4190,
             "distance_miles": 0.1},
            {"latitude": 37.8500, "longitude": -122.5000,
             "distance_miles": 8.0},
        ],
        "competition_summary": {"competitor_count_total": 2},
        "counts_by_bucket": {"hospital": {5: 2}},
        "counts_5mi": {"hospital": 2},
    }
    summary = apply_catchment_filter(profile, polygon)
    assert summary["applied"] is True
    assert summary["dropped"]["competitors"] == 1
    assert summary["dropped"]["demand_places"] == 1
    assert len(profile["competitors"]) == 1
    assert len(profile["demand_top_places"]["hospital"]) == 1
    assert profile["counts_5mi"]["hospital"] == 1
    assert profile["competition_summary"]["competitor_count_total"] == 1
    assert profile["catchment_polygon"] == polygon


def test_apply_catchment_filter_empty_polygon_is_noop():
    profile = {"competitors": [{"latitude": 1, "longitude": 1}]}
    summary = apply_catchment_filter(profile, polygon=[])
    assert summary["applied"] is False
    assert len(profile["competitors"]) == 1


def test_apply_catchment_filter_keeps_records_without_coords():
    polygon = [
        (37.7740, -122.4200), (37.7740, -122.4180),
        (37.7760, -122.4180), (37.7760, -122.4200),
    ]
    profile = {
        "demand_top_places": {"hospital": [{"name": "no-coords-here"}]},
        "competitors": [{"name": "ghost"}],
        "competition_summary": {},
        "counts_by_bucket": {},
        "counts_5mi": {},
    }
    apply_catchment_filter(profile, polygon)
    # Records without coords are kept (we don't want to drop name-only rows).
    assert len(profile["competitors"]) == 1
    assert len(profile["demand_top_places"]["hospital"]) == 1
