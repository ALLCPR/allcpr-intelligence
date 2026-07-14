"""
Demand-signal enricher.

For one candidate point, run Google Places across every DEMAND_CATEGORIES
entry. Returns:

  - places_by_category: {category_key: List[PlaceProfile]} (sorted by distance)
  - counts_by_bucket:   {category_key: {bucket_mi: count}}
  - top_places:         {category_key: List[PlaceProfile]} (capped per config)
  - sources:            list of source records for the SourceTracker
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from app.collectors.google_places import (
    GooglePlacesClient,
    miles_to_meters,
    to_place_profile,
)
from app.config import (
    DEMAND_CATEGORIES,
    DEMAND_TOP_N_PER_CATEGORY,
    DISTANCE_BUCKETS_MILES,
)
from app.models.place_profile import PlaceProfile
from app.utils.geo_utils import bucket_distances
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)


def collect_demand_for_point(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    radius_miles: float,
    categories: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, object]:
    categories = categories or DEMAND_CATEGORIES
    radius_m = miles_to_meters(radius_miles)

    places_by_category: Dict[str, List[PlaceProfile]] = {}
    counts_by_bucket: Dict[str, Dict[int, int]] = {}
    top_places: Dict[str, List[PlaceProfile]] = {}
    saturated_categories: List[str] = []

    for cat in categories:
        key = cat["key"]
        place_type = cat.get("type") or None
        keyword = cat.get("keyword") or None
        try:
            raw_results = client.nearby_search(
                origin,
                radius_meters=radius_m,
                place_type=place_type,
                keyword=keyword,
                max_pages=1,
            )
        except Exception as exc:
            logger.warning(f"demand category {key!r} failed: {exc}")
            counts_by_bucket[key] = {b: 0 for b in DISTANCE_BUCKETS_MILES}
            places_by_category[key] = []
            top_places[key] = []
            continue
        # Google Places legacy Nearby Search returns at most 20 results per
        # page. When max_pages=1 and we hit that ceiling, the actual count
        # in the area is *at least* 20 — the report should not present 20 as
        # a measured truth.
        if len(raw_results) >= 20:
            saturated_categories.append(key)

        category_places: List[PlaceProfile] = []
        seen_ids: set[str] = set()
        for raw in raw_results:
            pid = raw.get("place_id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            profile = to_place_profile(
                raw,
                category=key,
                origin=origin,
                source_query=keyword or place_type or key,
            )
            category_places.append(profile)

        category_places.sort(
            key=lambda p: (p.distance_miles if p.distance_miles is not None else 9999.0),
        )
        places_by_category[key] = category_places

        distances = [p.distance_miles for p in category_places
                     if p.distance_miles is not None]
        counts_by_bucket[key] = bucket_distances(distances, DISTANCE_BUCKETS_MILES)

        top_places[key] = category_places[:DEMAND_TOP_N_PER_CATEGORY]

    sources = [{
        "name": "Google Places API (Nearby Search)",
        "url": "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        "fields": [f"nearby_{c['key']}" for c in categories],
        "collected_at": utcnow_iso(),
        "notes": f"radius={radius_miles}mi",
    }]

    return {
        "places_by_category": places_by_category,
        "counts_by_bucket": counts_by_bucket,
        "top_places": top_places,
        "saturated_categories": saturated_categories,
        "sources": sources,
    }
