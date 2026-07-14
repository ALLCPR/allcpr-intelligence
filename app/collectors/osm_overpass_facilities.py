"""
OSM / Overpass community-facility enrichment → per-ZIP counts (offline).

Reads a PRE-FETCHED Overpass JSON (the ``{"elements": [...]}`` shape) and bins
nodes into ZIPs by nearest centroid. National OSM is huge and Overpass is
rate-limited, so this is build-time/offline only — never a dashboard call.
Run your own Overpass query per state/metro, save the JSON under data/raw/bulk/,
and point the build script at it.

Per-ZIP fields:
    childcare_count, school_count, community_facility_count,
    parking_proxy_score, commercial_access_proxy_score

The two proxy scores are rough 0–100 normalizations of parking / commercial
node density — DISPLAY-ONLY context, not validated access metrics.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.geo.zcta_join import assign_point_to_zcta, build_grid_index
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

_CHILDCARE = {"childcare", "kindergarten"}
_SCHOOL = {"school", "college", "university"}
_COMMUNITY = {"library", "community_centre", "community_center",
              "place_of_worship", "social_facility", "townhall"}

# Normalization caps for the proxy scores (count at/above cap → 100).
_PARKING_CAP = 10
_COMMERCIAL_CAP = 30


def _classify(tags: Dict[str, Any]) -> Optional[str]:
    amenity = str(tags.get("amenity") or "").lower()
    if amenity in _CHILDCARE:
        return "childcare"
    if amenity in _SCHOOL:
        return "school"
    if amenity in _COMMUNITY:
        return "community"
    if amenity == "parking":
        return "parking"
    if tags.get("shop") or amenity == "marketplace":
        return "commercial"
    return None


def parse_osm_elements(path: Path) -> List[Dict[str, Any]]:
    """Parse a pre-fetched Overpass JSON into ``[{lat, lng, kind}]``.

    Handles node ``lat/lon`` and way/relation ``center`` geometry. Missing /
    malformed file → ``[]``.
    """
    p = Path(path)
    if not p.exists():
        logger.warning(f"OSM: file not found, skipping: {p}")
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"OSM: failed to read {p}: {exc}")
        return []
    out: List[Dict[str, Any]] = []
    for el in data.get("elements", []):
        lat = el.get("lat")
        lng = el.get("lon")
        if lat is None and isinstance(el.get("center"), dict):
            lat = el["center"].get("lat")
            lng = el["center"].get("lon")
        if lat is None or lng is None:
            continue
        kind = _classify(el.get("tags") or {})
        if kind:
            out.append({"lat": float(lat), "lng": float(lng), "kind": kind})
    return out


def aggregate_osm_by_zip(
    elements: List[Dict[str, Any]],
    centroids: Dict[str, Tuple[float, float]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate OSM elements into per-ZIP counts + proxy access scores."""
    if not centroids:
        return {}
    index = build_grid_index(centroids)
    raw: Dict[str, Dict[str, int]] = {}
    for el in elements:
        z = assign_point_to_zcta(el["lat"], el["lng"], centroids, index=index)
        if not z:
            continue
        bucket = raw.setdefault(z, {})
        bucket[el["kind"]] = bucket.get(el["kind"], 0) + 1

    out: Dict[str, Dict[str, Any]] = {}
    for z, kinds in raw.items():
        childcare = kinds.get("childcare", 0)
        school = kinds.get("school", 0)
        community = childcare + school + kinds.get("community", 0)
        parking = kinds.get("parking", 0)
        commercial = kinds.get("commercial", 0)
        out[z] = {
            "childcare_count": childcare,
            "school_count": school,
            "community_facility_count": community,
            "parking_proxy_score": round(
                100.0 * min(1.0, parking / _PARKING_CAP), 1),
            "commercial_access_proxy_score": round(
                100.0 * min(1.0, commercial / _COMMERCIAL_CAP), 1),
        }
    return out


def load_osm(path: Path,
             centroids: Dict[str, Tuple[float, float]]) -> Dict[str, Dict[str, Any]]:
    """Parse + aggregate one Overpass JSON to ``{zip: {...}}``. ``{}`` if missing."""
    return aggregate_osm_by_zip(parse_osm_elements(path), centroids)
