"""
Catchment-polygon filtering for built profiles.

When a Mapbox drive-time isochrone polygon is available for a candidate, the
real catchment is the polygon — not the circular ``radius_miles`` used by the
Google Places queries. This module post-filters a built profile in place so
demand, competitor, and bucket counts only reflect places actually inside
the drive-time catchment.

The Google Places queries themselves still use ``radius_miles`` (Places
doesn't accept arbitrary polygons), so we over-fetch and trim — which is
the cheapest correct path.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.utils.commercial_zones import Polygon, point_in_polygon
from app.utils.geo_utils import bucket_distances
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


def _point_of(record: Any) -> Optional[Tuple[float, float]]:
    if hasattr(record, "latitude") and hasattr(record, "longitude"):
        lat = getattr(record, "latitude")
        lon = getattr(record, "longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    if isinstance(record, dict):
        lat = record.get("latitude") or record.get("lat")
        lon = record.get("longitude") or record.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    return None


def _filter_seq(records: Sequence[Any], polygon: Polygon) -> List[Any]:
    out: List[Any] = []
    for r in records:
        pt = _point_of(r)
        if pt is None:
            # No coords — keep it; we don't want to drop name-only records.
            out.append(r)
            continue
        if point_in_polygon(pt, polygon):
            out.append(r)
    return out


def apply_catchment_filter(
    profile: Dict[str, Any],
    polygon: Polygon,
    distance_buckets: Sequence[int] = (1, 3, 5, 10),
) -> Dict[str, Any]:
    """Drop demand/competitor records whose lat/lon fall outside ``polygon``.

    Mutates ``profile`` in place. Returns a small summary of how many records
    were trimmed per section so the report can surface "catchment trimmed N
    competitors / M demand drivers."
    """
    summary = {"applied": True, "dropped": {}}
    if not polygon or len(polygon) < 3:
        summary["applied"] = False
        return summary

    # ---- demand top_places + counts ---------------------------------------- #
    top_places = profile.get("demand_top_places") or {}
    new_top_places: Dict[str, List[Any]] = {}
    counts_by_bucket: Dict[str, Dict[int, int]] = {}
    total_dropped_demand = 0
    for key, lst in top_places.items():
        kept = _filter_seq(lst or [], polygon)
        dropped = len(lst or []) - len(kept)
        total_dropped_demand += dropped
        new_top_places[key] = kept
        # Re-derive bucket counts from kept items' distances.
        dists: List[float] = []
        for r in kept:
            d = (r.get("distance_miles") if isinstance(r, dict)
                 else getattr(r, "distance_miles", None))
            if isinstance(d, (int, float)):
                dists.append(float(d))
        counts_by_bucket[key] = bucket_distances(dists, distance_buckets)
    profile["demand_top_places"] = new_top_places
    profile["counts_by_bucket"] = counts_by_bucket
    profile["counts_5mi"] = {
        k: counts_by_bucket.get(k, {}).get(5, 0)
        for k in counts_by_bucket
    }
    summary["dropped"]["demand_places"] = total_dropped_demand

    # ---- competitors ------------------------------------------------------- #
    competitors = profile.get("competitors") or []
    kept_competitors = _filter_seq(competitors, polygon)
    dropped_competitors = len(competitors) - len(kept_competitors)
    profile["competitors"] = kept_competitors
    profile["competitors_sample"] = kept_competitors[:10]

    # Recompute the competition_summary buckets.
    comp_summary = dict(profile.get("competition_summary") or {})
    dists = []
    for c in kept_competitors:
        d = (c.get("distance_miles") if isinstance(c, dict)
             else getattr(c, "distance_miles", None))
        if isinstance(d, (int, float)):
            dists.append(float(d))
    comp_summary["competitor_count_total"] = len(kept_competitors)
    comp_summary["competitor_count_by_bucket_mi"] = bucket_distances(
        dists, distance_buckets,
    )
    profile["competition_summary"] = comp_summary
    summary["dropped"]["competitors"] = dropped_competitors

    profile["catchment_polygon"] = polygon
    profile["catchment_filter"] = summary
    return summary
