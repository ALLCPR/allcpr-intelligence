"""
Mapbox Isochrone API — drive-time catchment polygons.

A 5-mile circle around downtown SF crosses the Bay; a 10-minute drive
catchment doesn't. For dense urban site selection the difference is huge:
the demand catchment is whatever a customer can actually drive to in 10
minutes, not whatever sits inside a circle. Mapbox's isochrone API is the
cheapest viable source for this (100k free requests / month).

This module is feature-flagged via ``MAPBOX_TOKEN``. When the token is
absent the collector is a no-op, ``isochrone_polygon`` returns ``None``,
and the rest of the pipeline keeps using the existing circular radius.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

MAPBOX_TOKEN: str = os.getenv("MAPBOX_TOKEN", "")
MAPBOX_ISOCHRONE_BASE = "https://api.mapbox.com/isochrone/v1/mapbox"

# Catchment polygons are stable for months; cache long.
MAPBOX_ISOCHRONE_TTL_SECONDS = 90 * 86400

VALID_PROFILES = ("driving", "driving-traffic", "walking", "cycling")
MAX_CONTOUR_MINUTES = 60


def is_configured() -> bool:
    return bool(MAPBOX_TOKEN)


def fetch_isochrone(
    origin: Tuple[float, float],
    minutes: int = 10,
    profile: str = "driving",
    cache: Optional[Cache] = None,
) -> Optional[List[Tuple[float, float]]]:
    """Return the isochrone polygon for ``origin`` as ``[(lat, lon), ...]``.

    ``None`` is returned when:
    - Mapbox is not configured (no token),
    - the request fails,
    - the response carries no polygon.

    Callers should treat ``None`` as "fall back to the circular radius".
    """
    if not MAPBOX_TOKEN:
        return None
    if profile not in VALID_PROFILES:
        logger.warning(f"mapbox: invalid profile {profile!r}")
        return None
    minutes = max(1, min(int(minutes), MAX_CONTOUR_MINUTES))
    lat, lon = origin
    params = {
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "minutes": minutes,
        "profile": profile,
    }

    def _live() -> Optional[List[Tuple[float, float]]]:
        return _live_isochrone(lat, lon, minutes, profile)

    value, _ = cached_call(
        cache,
        provider="mapbox",
        method=f"isochrone_{profile}_{minutes}",
        params=params,
        ttl_seconds=MAPBOX_ISOCHRONE_TTL_SECONDS,
        live_call=_live,
    )
    return value


def _live_isochrone(
    lat: float, lon: float, minutes: int, profile: str
) -> Optional[List[Tuple[float, float]]]:
    url = f"{MAPBOX_ISOCHRONE_BASE}/{profile}/{lon},{lat}"
    params = {
        "contours_minutes": str(minutes),
        "polygons": "true",
        "denoise": "1",
        "access_token": MAPBOX_TOKEN,
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning(f"mapbox: isochrone request failed: {exc}")
        return None
    if resp.status_code >= 400:
        # Strip token from error message for safety.
        preview = (resp.text or "")[:200].replace(MAPBOX_TOKEN, "<token>")
        logger.warning(f"mapbox: HTTP {resp.status_code}: {preview!r}")
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("mapbox: non-JSON body")
        return None
    features = data.get("features") or []
    if not features:
        return None
    geom = (features[0] or {}).get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None
    rings = geom.get("coordinates") or []
    if not rings or not rings[0]:
        return None
    # Mapbox returns [lon, lat] pairs; we use (lat, lon) everywhere else.
    return [(lat, lon) for lon, lat in rings[0]]
