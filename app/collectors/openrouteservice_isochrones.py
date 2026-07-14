"""
OpenRouteService (ORS) isochrone collector — card-free Mapbox alternative.

Mapbox requires a credit card to activate even the free tier. ORS doesn't:
sign up at openrouteservice.org with just an email, get a free key, and you
get 500 isochrone requests/day. ORS is OpenStreetMap-based, which is
consistent with the OSM commercial-zone filtering already in this pipeline.

Exposes the same interface as ``mapbox_isochrones`` so the pipeline can use
whichever provider is configured:
  - ``is_configured() -> bool``
  - ``fetch_isochrone(origin, minutes, profile, cache) -> Optional[List[(lat, lon)]]``

Feature-flagged on ``ORS_API_KEY``. Absent → returns ``None``, pipeline falls
back to Mapbox (if configured) or the circular radius.

ORS isochrones docs:
https://openrouteservice.org/dev/#/api-docs/v2/isochrones
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

ORS_API_KEY: str = os.getenv("ORS_API_KEY", "")
ORS_BASE = "https://api.openrouteservice.org/v2/isochrones"
ORS_ISOCHRONE_TTL_SECONDS = 90 * 86400
ORS_MAX_MINUTES = 60

# Map the pipeline's Mapbox-style profile names to ORS profile slugs.
# ORS has no live-traffic profile on the free tier, so driving-traffic
# degrades to driving-car.
_PROFILE_MAP = {
    "driving": "driving-car",
    "driving-traffic": "driving-car",
    "walking": "foot-walking",
    "cycling": "cycling-regular",
}


def is_configured() -> bool:
    return bool(ORS_API_KEY)


def fetch_isochrone(
    origin: Tuple[float, float],
    minutes: int = 10,
    profile: str = "driving",
    cache: Optional[Cache] = None,
) -> Optional[List[Tuple[float, float]]]:
    """Return the isochrone polygon for ``origin`` as ``[(lat, lon), ...]``.

    ``None`` when ORS isn't configured, the request fails, or no polygon is
    returned — caller should fall back to another provider or the circular
    radius. Profile names match the Mapbox adapter (``driving`` /
    ``driving-traffic`` / ``walking`` / ``cycling``) and are mapped to ORS
    slugs internally.
    """
    if not ORS_API_KEY:
        return None
    ors_profile = _PROFILE_MAP.get(profile, "driving-car")
    minutes = max(1, min(int(minutes), ORS_MAX_MINUTES))
    lat, lon = origin
    params = {
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "minutes": minutes,
        "profile": ors_profile,
    }

    def _live() -> Optional[List[Tuple[float, float]]]:
        return _live_isochrone(lat, lon, minutes, ors_profile)

    value, _ = cached_call(
        cache,
        provider="ors",
        method=f"isochrone_{ors_profile}_{minutes}",
        params=params,
        ttl_seconds=ORS_ISOCHRONE_TTL_SECONDS,
        live_call=_live,
    )
    return value


def _live_isochrone(
    lat: float, lon: float, minutes: int, ors_profile: str
) -> Optional[List[Tuple[float, float]]]:
    url = f"{ORS_BASE}/{ors_profile}"
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/geo+json",
    }
    body = {
        "locations": [[lon, lat]],          # ORS takes [lon, lat]
        "range": [minutes * 60],            # seconds
        "range_type": "time",
    }
    try:
        resp = requests.post(url, json=body, headers=headers,
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning(f"ors: isochrone request failed: {exc}")
        return None
    if resp.status_code >= 400:
        preview = (resp.text or "")[:200]
        if ORS_API_KEY and ORS_API_KEY in preview:
            preview = preview.replace(ORS_API_KEY, "<key>")
        logger.warning(f"ors: HTTP {resp.status_code}: {preview!r}")
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("ors: non-JSON body")
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
    # ORS returns [lon, lat] pairs; convert to (lat, lon).
    return [(pt[1], pt[0]) for pt in rings[0] if len(pt) >= 2]
