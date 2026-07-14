"""Accessibility proxy collection.

Exact parking, traffic, and walkability usually require paid or specialized
datasets. This module collects conservative proxy signals from Places and
marks anything not directly observed as unknown.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from app.collectors.google_places import (
    GooglePlacesClient,
    miles_to_meters,
    to_place_profile,
)
from app.models.place_profile import PlaceProfile
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)


def _nearest(places: List[PlaceProfile]) -> Optional[PlaceProfile]:
    places = [p for p in places if p.distance_miles is not None]
    if not places:
        return None
    return sorted(places, key=lambda p: p.distance_miles or 9999.0)[0]


def _signal_from_place(place: Optional[PlaceProfile], label: str,
                       confidence: str = "proxy") -> Dict[str, object]:
    if place is None:
        return {
            "status": "unknown",
            "confidence": "unknown",
            "nearest_name": "unknown",
            "distance_miles": None,
            "notes": f"{label} not observed in proxy search",
        }
    return {
        "status": "detected",
        "confidence": confidence,
        "nearest_name": place.name or "unknown",
        "distance_miles": place.distance_miles,
        "google_maps_url": place.maps_url_fallback,
        "notes": f"nearest {label} proxy",
    }


def _search(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    *,
    radius_miles: float,
    category: str,
    place_type: str = "",
    keyword: str = "",
) -> List[PlaceProfile]:
    try:
        raw = client.nearby_search(
            origin,
            radius_meters=miles_to_meters(radius_miles),
            place_type=place_type or None,
            keyword=keyword or None,
            max_pages=1,
        )
    except Exception as exc:
        logger.warning(f"accessibility search failed ({category}): {exc}")
        return []
    out: List[PlaceProfile] = []
    seen: set[str] = set()
    for item in raw:
        pid = item.get("place_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(to_place_profile(
            item,
            category=f"accessibility:{category}",
            origin=origin,
            source_query=keyword or place_type or category,
        ))
    out.sort(key=lambda p: p.distance_miles if p.distance_miles is not None else 9999.0)
    return out


def collect_accessibility_for_point(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    radius_miles: float,
    counts_by_bucket: Optional[Dict[str, Dict[int, int]]] = None,
) -> Dict[str, object]:
    """Collect proxy accessibility signals for a candidate point."""
    transit = _search(
        client, origin, radius_miles=min(max(radius_miles, 1.0), 3.0),
        category="transit_station", place_type="transit_station",
    )
    airports = _search(
        client, origin, radius_miles=min(max(radius_miles, 5.0), 10.0),
        category="airport", place_type="airport",
    )
    shopping = (
        _search(client, origin, radius_miles=1.25,
                category="shopping_center", place_type="shopping_mall")
        + _search(client, origin, radius_miles=1.25,
                  category="shopping_plaza", keyword="shopping center plaza")
    )
    parking = _search(
        client, origin, radius_miles=0.5, category="parking", place_type="parking",
    )
    business = _search(
        client, origin, radius_miles=min(max(radius_miles, 2.0), 5.0),
        category="business_corridor", keyword="business park office park",
    )
    freeway = _search(
        client, origin, radius_miles=1.5,
        category="freeway_major_road", keyword="freeway exit major road",
    )

    one_mile_total = 0
    if counts_by_bucket:
        one_mile_total = sum(c.get(1, 0) for c in counts_by_bucket.values())
    walkability_status = "detected" if one_mile_total >= 8 else "unknown"
    walkability = {
        "status": walkability_status,
        "confidence": "proxy" if walkability_status == "detected" else "unknown",
        "nearby_places_1mi": one_mile_total,
        "notes": (
            "proxy from count of nearby demand/commercial places within 1 mile"
            if one_mile_total
            else "walkability not measured; no walk score source integrated"
        ),
    }

    parking_place = _nearest(parking) or _nearest(shopping)
    parking_signal = _signal_from_place(parking_place, "parking/plaza", "proxy")
    parking_signal["notes"] = (
        "Exact parking is unknown; this is a proxy from nearby parking, "
        "shopping plazas, or commercial areas."
        if parking_place
        else "Exact parking is unknown; no parking/plaza proxy observed."
    )

    signals = {
        "freeway_major_road_proximity": _signal_from_place(
            _nearest(freeway), "freeway/major road", "proxy",
        ),
        "transit_station_proximity": _signal_from_place(
            _nearest(transit), "transit station", "proxy",
        ),
        "airport_business_corridor_proximity": _signal_from_place(
            _nearest(airports) or _nearest(business), "airport/business corridor", "proxy",
        ),
        "shopping_center_plaza_proximity": _signal_from_place(
            _nearest(shopping), "shopping center/plaza", "proxy",
        ),
        "parking_proxy": parking_signal,
        "walkability_proxy": walkability,
    }

    populated = [
        key for key, value in signals.items()
        if isinstance(value, dict) and value.get("status") == "detected"
    ]
    return {
        "signals": signals,
        "sources": [{
            "name": "Google Places API (Nearby Search)",
            "url": "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            "fields": [f"accessibility.{k}" for k in populated],
            "collected_at": utcnow_iso(),
            "notes": "accessibility proxy searches; exact parking remains unknown unless observed",
        }],
    }
