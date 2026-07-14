"""Tests for the centroid-based ZCTA spatial join."""
from __future__ import annotations

from app.geo import zcta_join as zj

# Three well-separated ZIP centroids.
CENTROIDS = {
    "95112": (37.33, -121.88),   # San Jose
    "97202": (45.48, -122.64),   # Portland
    "10001": (40.75, -73.99),    # NYC
}


def test_assign_point_to_nearest():
    # A point right next to San Jose's centroid.
    assert zj.assign_point_to_zcta(37.331, -121.881, CENTROIDS) == "95112"
    # A point near Portland.
    assert zj.assign_point_to_zcta(45.49, -122.65, CENTROIDS) == "97202"


def test_assign_returns_none_when_too_far():
    # Middle of the ocean — nothing within max_miles.
    assert zj.assign_point_to_zcta(0.0, 0.0, CENTROIDS, max_miles=25) is None


def test_assign_none_coords_safe():
    assert zj.assign_point_to_zcta(None, None, CENTROIDS) is None
    assert zj.assign_point_to_zcta(1.0, 2.0, {}) is None


def test_aggregate_points_by_zcta():
    pts = [(37.33, -121.88), (37.34, -121.89),   # 2 near San Jose
           (45.48, -122.64),                       # 1 near Portland
           (0.0, 0.0)]                             # dropped (too far)
    counts = zj.aggregate_points_by_zcta(pts, CENTROIDS)
    assert counts["95112"] == 2
    assert counts["97202"] == 1
    assert "10001" not in counts


def test_grid_index_matches_bruteforce():
    # With or without a prebuilt index, the assignment agrees.
    idx = zj.build_grid_index(CENTROIDS)
    for lat, lng in [(37.0, -121.5), (45.5, -122.6), (40.7, -74.0)]:
        assert (zj.assign_point_to_zcta(lat, lng, CENTROIDS, index=idx)
                == zj.assign_point_to_zcta(lat, lng, CENTROIDS))
