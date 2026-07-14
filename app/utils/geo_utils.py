"""
Geographic helpers: haversine distance, geocoding, candidate-grid generation.

Coordinates throughout the project are decimal degrees (lat, lon).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import requests

from app.config import (
    GOOGLE_MAPS_API_KEY,
    REQUEST_TIMEOUT,
    GRID_SPACING_MILES,
    MAX_CANDIDATES_PER_CITY,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

EARTH_RADIUS_MI = 3958.7613

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_GEOCODE_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float

    def as_tuple(self) -> Tuple[float, float]:
        return (self.lat, self.lon)


def haversine_miles(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points in miles."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(h))


def offset_latlon(origin: Tuple[float, float], north_mi: float, east_mi: float
                  ) -> Tuple[float, float]:
    """Offset a (lat, lon) by miles north/east. Approximate; fine for city-scale grids."""
    lat, lon = origin
    dlat = north_mi / 69.0
    # 1 deg longitude ~= 69 * cos(lat) miles
    dlon = east_mi / (69.0 * math.cos(math.radians(lat)) or 1e-9)
    return (lat + dlat, lon + dlon)


def geocode_city(city: str, state: str = "", country: str = "US"
                 ) -> Optional[LatLon]:
    """
    Resolve a city name to a (lat, lon) via Google Geocoding API.
    Returns None on failure. The caller should treat None as "unknown."
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.warning("geocode_city: GOOGLE_MAPS_API_KEY not set; cannot geocode.")
        return None
    parts = [city]
    if state:
        parts.append(state)
    if country:
        parts.append(country)
    address = ", ".join(parts)
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": address, "key": GOOGLE_MAPS_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning(f"geocode_city: network error for {address!r}: {exc}")
        return None
    if data.get("status") != "OK" or not data.get("results"):
        logger.warning(
            f"geocode_city: no Geocoding API match for {address!r} "
            f"(status={data.get('status')}); trying Places API (New)."
        )
        return _geocode_city_with_places_text(address)
    loc = data["results"][0]["geometry"]["location"]
    return LatLon(lat=loc["lat"], lon=loc["lng"])


def _geocode_city_with_places_text(address: str) -> Optional[LatLon]:
    """Fallback city-center lookup for keys that only have Places API (New)."""
    try:
        resp = requests.post(
            PLACES_TEXT_URL,
            json={"textQuery": address, "pageSize": 1},
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": PLACES_GEOCODE_FIELD_MASK,
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        resp.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"geocode_city: Places API fallback failed for {address!r}: {exc}")
        return None
    places = data.get("places") or []
    if not places:
        logger.warning(f"geocode_city: Places API fallback found no match for {address!r}")
        return None
    loc = places[0].get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        logger.warning(f"geocode_city: Places API fallback returned no location for {address!r}")
        return None
    return LatLon(lat=float(lat), lon=float(lon))


def generate_grid(center: Tuple[float, float],
                  radius_miles: float,
                  spacing_miles: Optional[float] = None,
                  max_points: int = MAX_CANDIDATES_PER_CITY,
                  ) -> List[Tuple[float, float]]:
    """
    Build a square grid of candidate points around `center`, clipped to a circle
    of `radius_miles`. Always includes the center as the first point.
    """
    spacing = spacing_miles if spacing_miles is not None else GRID_SPACING_MILES
    if spacing <= 0:
        return [center]
    pts: List[Tuple[float, float]] = []
    steps = int(math.ceil(radius_miles / spacing))
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            north = i * spacing
            east = j * spacing
            if math.hypot(north, east) > radius_miles:
                continue
            pts.append(offset_latlon(center, north, east))
    # Sort by distance from center so the center point comes first.
    pts.sort(key=lambda p: haversine_miles(center, p))
    return pts[:max_points]


def bucket_distances(distances: Iterable[float], buckets: Iterable[int]
                     ) -> dict[int, int]:
    """Count how many distances fall within each cumulative mile bucket."""
    buckets = sorted(buckets)
    counts = {b: 0 for b in buckets}
    for d in distances:
        for b in buckets:
            if d <= b:
                counts[b] += 1
    return counts
