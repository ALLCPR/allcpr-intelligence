"""
OpenStreetMap commercial-zone collector.

OSM tags `landuse=commercial`, `landuse=retail`, and `building=commercial|
retail|office|mall|supermarket` reliably mark land that can host a leasable
storefront. Google Places carries no equivalent signal, so this module is
the missing piece for "is this candidate point actually in a commercial
zone or did it land in a residential block?"

Usage:
    polygons = fetch_commercial_polygons(
        bbox=(south, west, north, east),
        cache=cache,
    )

Each polygon is ``List[Tuple[float, float]]`` of (lat, lon) vertices, which
is what ``app.utils.commercial_zones`` consumes.

The free Overpass instances rate-limit aggressively. The collector caches
aggressively (30-day TTL) and tolerates failure — when the Overpass query
errors or times out, an empty list is returned and the pipeline falls back
to no-filter behavior with a confidence hit.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import requests

from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

OVERPASS_URL = os.getenv(
    "OVERPASS_URL", "https://overpass-api.de/api/interpreter"
)
OSM_REQUEST_TIMEOUT = int(os.getenv("OSM_REQUEST_TIMEOUT", "60"))
OSM_CACHE_TTL_SECONDS = 30 * 86400  # 30 days; commercial zoning changes slowly

OSM_ZONING_ENABLED = os.getenv("OSM_ZONING_ENABLED", "true").lower() != "false"

# Polygon = list of (lat, lon) vertices
Polygon = List[Tuple[float, float]]


_OVERPASS_QUERY = """
[out:json][timeout:{timeout}];
(
  way["landuse"~"commercial|retail"]({south},{west},{north},{east});
  way["building"~"commercial|retail|office|mall|supermarket"]({south},{west},{north},{east});
  relation["landuse"~"commercial|retail"]({south},{west},{north},{east});
);
out geom;
""".strip()


def _round_bbox(
    bbox: Tuple[float, float, float, float], precision: int = 3
) -> Tuple[float, float, float, float]:
    """Round bbox to ~100m precision for cache-key stability."""
    return tuple(round(x, precision) for x in bbox)  # type: ignore[return-value]


def fetch_commercial_polygons(
    bbox: Tuple[float, float, float, float],
    cache: Optional[Cache] = None,
) -> List[Polygon]:
    """Fetch commercial / retail / office polygons within ``bbox``.

    ``bbox`` is ``(south, west, north, east)``. Returns an empty list when
    OSM is disabled, the request fails, or the bbox is invalid.
    """
    if not OSM_ZONING_ENABLED:
        return []
    south, west, north, east = bbox
    if not (south < north and west < east):
        logger.warning(f"osm_zoning: invalid bbox {bbox}")
        return []
    rounded = _round_bbox(bbox)
    params = {"bbox": list(rounded)}

    def _live() -> List[Polygon]:
        return _fetch_live(rounded)

    value, _ = cached_call(
        cache,
        provider="osm",
        method="commercial_polygons",
        params=params,
        ttl_seconds=OSM_CACHE_TTL_SECONDS,
        live_call=_live,
    )
    return value or []


def _fetch_live(bbox: Tuple[float, float, float, float]) -> List[Polygon]:
    south, west, north, east = bbox
    query = _OVERPASS_QUERY.format(
        timeout=OSM_REQUEST_TIMEOUT,
        south=south, west=west, north=north, east=east,
    )
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=OSM_REQUEST_TIMEOUT + 5,
            headers={"User-Agent": "ALLCPR-Site-Intel/1.0 (commercial-zoning lookup)"},
        )
    except requests.RequestException as exc:
        logger.warning(f"osm_zoning: Overpass request failed: {exc}")
        return []
    if resp.status_code >= 400:
        logger.warning(
            f"osm_zoning: Overpass HTTP {resp.status_code}: "
            f"{(resp.text or '')[:200]!r}"
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("osm_zoning: Overpass returned non-JSON body")
        return []

    polygons: List[Polygon] = []
    for element in data.get("elements") or []:
        kind = element.get("type")
        if kind == "way":
            geom = element.get("geometry") or []
            ring: Polygon = [
                (g["lat"], g["lon"]) for g in geom
                if isinstance(g, dict) and "lat" in g and "lon" in g
            ]
            if len(ring) >= 3:
                polygons.append(ring)
        elif kind == "relation":
            for member in element.get("members") or []:
                if member.get("role") not in ("outer", "outline"):
                    continue
                geom = member.get("geometry") or []
                ring = [
                    (g["lat"], g["lon"]) for g in geom
                    if isinstance(g, dict) and "lat" in g and "lon" in g
                ]
                if len(ring) >= 3:
                    polygons.append(ring)
    logger.info(
        f"osm_zoning: fetched {len(polygons)} commercial polygon(s) for bbox "
        f"({south:.3f},{west:.3f},{north:.3f},{east:.3f})"
    )
    return polygons


def is_available() -> bool:
    return OSM_ZONING_ENABLED
