"""
Point-in-polygon helpers for commercial-zoning filtering.

Polygons here are stored as plain ``List[Tuple[float, float]]`` of (lat, lon)
vertices — same shape as what the OSM zoning collector produces. No external
dependencies on shapely so the build stays light; the implementations below
are exact for the standard ray-cast and great-circle-approximated distance,
which is what we need for "is this candidate inside a commercial polygon?
if not, is it within 250m of one?".
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]  # (lat, lon)
Polygon = List[Point]


_EARTH_RADIUS_METERS = 6_371_000.0


def haversine_meters(a: Point, b: Point) -> float:
    """Great-circle distance between two (lat, lon) points, in meters."""
    lat1, lon1 = a
    lat2, lon2 = b
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    s = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(s), math.sqrt(1 - s))
    return _EARTH_RADIUS_METERS * c


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray-cast inclusion test. ``polygon`` is a list of (lat, lon) vertices,
    closing edge implied. Edge / vertex cases follow the standard 'crossing
    number' convention.
    """
    if len(polygon) < 3:
        return False
    lat, lon = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        # Standard ray-cast in (lon, lat) plane treating coords as cartesian.
        # At this scale errors are negligible for inclusion testing.
        if ((lon_i > lon) != (lon_j > lon)) and (
            lat < (lat_j - lat_i) * (lon - lon_i) / ((lon_j - lon_i) or 1e-12) + lat_i
        ):
            inside = not inside
        j = i
    return inside


def distance_to_polygon_meters(point: Point, polygon: Polygon) -> float:
    """Approximate distance from ``point`` to the nearest edge of ``polygon``.

    Returns 0.0 when the point is inside. For points outside, computes the
    minimum great-circle distance to each polygon vertex — coarse but
    sufficient for the "within 250m of a commercial zone" check.
    """
    if point_in_polygon(point, polygon):
        return 0.0
    if not polygon:
        return float("inf")
    return min(haversine_meters(point, v) for v in polygon)


def point_within_any_polygon(
    point: Point,
    polygons: Sequence[Polygon],
    max_distance_meters: float = 0.0,
) -> bool:
    """True when ``point`` lies inside one of ``polygons`` or within
    ``max_distance_meters`` of one of them.

    ``max_distance_meters=0`` is a strict inside-only check.
    """
    if not polygons:
        return False
    for poly in polygons:
        if point_in_polygon(point, poly):
            return True
    if max_distance_meters <= 0:
        return False
    for poly in polygons:
        if distance_to_polygon_meters(point, poly) <= max_distance_meters:
            return True
    return False


def bbox_for_radius(
    center: Point, radius_miles: float
) -> Tuple[float, float, float, float]:
    """Return ``(south, west, north, east)`` bounding box for the given
    circle. Latitude conversion is constant (~69 mi / deg); longitude
    correction uses ``cos(latitude)``.
    """
    lat, lon = center
    dlat = radius_miles / 69.0
    dlon = radius_miles / (69.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def filter_points_to_commercial(
    points: Sequence[Point],
    polygons: Sequence[Polygon],
    max_distance_meters: float = 250.0,
    min_keep: int = 3,
) -> Tuple[List[Point], List[Point]]:
    """Split ``points`` into (kept, dropped) where kept are inside-or-near a
    commercial polygon. When fewer than ``min_keep`` points survive the
    filter (sparse OSM coverage), the original list is returned unchanged
    so the pipeline doesn't end up with zero candidates.
    """
    if not polygons:
        return list(points), []
    kept: List[Point] = []
    dropped: List[Point] = []
    for p in points:
        if point_within_any_polygon(
            p, polygons, max_distance_meters=max_distance_meters
        ):
            kept.append(p)
        else:
            dropped.append(p)
    if len(kept) < min_keep:
        # Fall back to no-filter — better to keep all candidates with a
        # confidence hit than to return an empty list.
        return list(points), []
    return kept, dropped
