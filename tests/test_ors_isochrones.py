"""OpenRouteService isochrone adapter tests.

Mirrors the Mapbox adapter's contract:
- feature-flag fallback when ORS_API_KEY is unset
- GeoJSON Polygon → [(lat, lon), ...] conversion (ORS returns [lon, lat])
- profile-name mapping (driving → driving-car, etc.)
- graceful None on HTTP error / empty features
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import openrouteservice_isochrones as ors  # noqa: E402


def test_not_configured_returns_none(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "")
    assert ors.is_configured() is False
    assert ors.fetch_isochrone((37.7749, -122.4194), minutes=10) is None


def test_profile_mapping_covers_pipeline_names():
    assert ors._PROFILE_MAP["driving"] == "driving-car"
    assert ors._PROFILE_MAP["driving-traffic"] == "driving-car"
    assert ors._PROFILE_MAP["walking"] == "foot-walking"
    assert ors._PROFILE_MAP["cycling"] == "cycling-regular"


def test_polygon_lonlat_converted_to_latlon(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "test-key")
    # ORS returns coordinates as [lon, lat]; our pipeline wants (lat, lon).
    fake_polygon = [
        [-122.420, 37.774],
        [-122.418, 37.774],
        [-122.418, 37.776],
        [-122.420, 37.776],
    ]
    with patch.object(
        ors, "_live_isochrone",
        return_value=[(pt[1], pt[0]) for pt in fake_polygon],
    ):
        poly = ors.fetch_isochrone((37.775, -122.419), minutes=10)
    assert poly is not None
    assert poly[0] == (37.774, -122.420)  # (lat, lon) order
    assert all(37.0 < lat < 38.0 and -123.0 < lon < -122.0 for lat, lon in poly)


def test_live_isochrone_parses_geojson(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "test-key")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [-122.420, 37.774],
                                [-122.418, 37.774],
                                [-122.418, 37.776],
                            ]],
                        },
                    }
                ]
            }

    with patch("app.collectors.openrouteservice_isochrones.requests.post",
               return_value=_Resp()):
        poly = ors._live_isochrone(37.775, -122.419, 10, "driving-car")
    assert poly == [(37.774, -122.420), (37.774, -122.418), (37.776, -122.418)]


def test_live_isochrone_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "test-key")

    class _Resp:
        status_code = 403
        text = '{"error":"quota exceeded"}'

    with patch("app.collectors.openrouteservice_isochrones.requests.post",
               return_value=_Resp()):
        assert ors._live_isochrone(37.775, -122.419, 10, "driving-car") is None


def test_live_isochrone_empty_features_returns_none(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "test-key")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"features": []}

    with patch("app.collectors.openrouteservice_isochrones.requests.post",
               return_value=_Resp()):
        assert ors._live_isochrone(37.775, -122.419, 10, "driving-car") is None


def test_minutes_clamped_to_valid_range(monkeypatch):
    monkeypatch.setattr(ors, "ORS_API_KEY", "test-key")
    captured = {}

    def _fake_live(lat, lon, minutes, profile):
        captured["minutes"] = minutes
        return [(37.0, -122.0), (37.1, -122.0), (37.1, -122.1)]

    with patch.object(ors, "_live_isochrone", side_effect=_fake_live):
        ors.fetch_isochrone((37.0, -122.0), minutes=999)
    assert captured["minutes"] == ors.ORS_MAX_MINUTES
