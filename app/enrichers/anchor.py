"""
Candidate anchor selection.

A "candidate" point on the grid is just a (lat, lon). On its own that's not
useful in a report. This module finds the closest meaningful real-world anchor
— a shopping plaza, office building, transit hub, hospital, etc. — so the
report can refer to the area by name and address.

Falls back to reverse-geocoded street address if no nearby anchor matches.
"""
from __future__ import annotations

from typing import Optional, Tuple

import requests

from app.collectors import foursquare_places
from app.collectors.google_places import (
    GooglePlacesClient,
    miles_to_meters,
    to_place_profile,
)
from app.config import (
    ANCHOR_MAX_DISTANCE_MILES,
    ANCHOR_QUERIES,
    GOOGLE_MAPS_API_KEY,
    REQUEST_TIMEOUT,
)
from app.models.place_profile import PlaceProfile
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger
from app.utils.viability_filter import has_commercial_signal, is_anchor_viable

logger = get_logger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

ANCHOR_SEARCH_RADIUS_MILES = 0.75  # how far to look for a named anchor


def _reverse_geocode(latitude: float, longitude: float
                     ) -> Optional[PlaceProfile]:
    """Resolve a coordinate to a street address via Google Geocoding API."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={
                "latlng": f"{latitude},{longitude}",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"reverse_geocode failed: {exc}")
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    first = data["results"][0]
    return PlaceProfile(
        place_id=first.get("place_id", "") or "",
        name=first.get("formatted_address", "") or "address",
        formatted_address=first.get("formatted_address", "") or "",
        latitude=latitude,
        longitude=longitude,
        category="anchor:reverse_geocode",
        source_query="reverse_geocode",
        google_maps_url=(
            f"https://www.google.com/maps/place/?q=place_id:{first.get('place_id')}"
            if first.get("place_id") else
            f"https://www.google.com/maps?q={latitude},{longitude}"
        ),
        confidence="api",
        source_api="Google Geocoding API",
    )


def _foursquare_pool(
    origin: Tuple[float, float],
    radius_miles: float,
    max_distance_miles: float,
    cache,
) -> list:
    """Foursquare commercial-anchor pool. Empty when not configured."""
    if not foursquare_places.is_configured():
        return []
    try:
        candidates = foursquare_places.search_commercial_anchors(
            origin=origin, radius_miles=radius_miles, cache=cache,
        )
    except Exception as exc:
        logger.warning(f"anchor: Foursquare lookup failed: {exc}")
        return []
    pool: list = []
    for candidate in candidates:
        dist = candidate.distance_miles
        if dist is None or dist > max_distance_miles:
            continue
        pool.append((dist, candidate.types[0] if candidate.types else "foursquare", candidate))
    pool.sort(key=lambda t: t[0])
    if pool:
        logger.info(f"anchor: Foursquare returned {len(pool)} commercial "
                    f"candidate(s) within {max_distance_miles}mi")
    return pool


def select_anchor(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    radius_miles: float = ANCHOR_SEARCH_RADIUS_MILES,
    max_distance_miles: float = ANCHOR_MAX_DISTANCE_MILES,
) -> Optional[PlaceProfile]:
    """
    Find the closest meaningful anchor place near `origin`. Returns a hydrated
    PlaceProfile (with phone/website/hours when available), or None.

    Strategy: query every ANCHOR_QUERIES category, collect all hits within
    `max_distance_miles`, and pick the closest one. This avoids the trap of
    locking onto a far-away shopping mall when a much closer transit station
    or office building is available.

    Falls back to reverse-geocoded street address if no nearby anchor matches.
    """
    cache = getattr(client, "_cache", None)
    # Foursquare-first pool: its clean commercial taxonomy is what Google's
    # type tags can't give us. When the closest Foursquare hit is a real
    # Shopping Mall / Medical Center / Office Building, use it. Falls back
    # to the Google-anchor pool below when FOURSQUARE_API_KEY is unset or
    # no commercial candidate is within reach.
    fsq_pool = _foursquare_pool(origin, radius_miles, max_distance_miles, cache)
    if fsq_pool:
        for dist, label, candidate in fsq_pool:
            viable, _ = is_anchor_viable(
                types=candidate.types,
                name=candidate.name,
                formatted_address=candidate.formatted_address,
            )
            if not viable:
                continue
            # Foursquare-sourced anchors don't have Google place_ids, so
            # hydrate_with_details would fail. Foursquare's search result
            # already gives us name + coords + category; details aren't
            # needed for the report's anchor card.
            logger.info(f"anchor selected (Foursquare): {candidate.name} "
                        f"({label}, {candidate.distance_miles}mi)")
            return candidate

    radius_m = miles_to_meters(radius_miles)
    pool: list = []
    for cfg in ANCHOR_QUERIES:
        place_type = cfg.get("type") or None
        keyword = cfg.get("keyword") or None
        try:
            raw_results = client.nearby_search(
                origin,
                radius_meters=radius_m,
                place_type=place_type,
                keyword=keyword,
                max_pages=1,
            )
        except Exception as exc:
            logger.warning(f"anchor query failed ({cfg}): {exc}")
            continue
        for raw in raw_results:
            if not raw.get("place_id"):
                continue
            candidate = to_place_profile(
                raw,
                category=f"anchor:{cfg.get('label', '')}",
                origin=origin,
                source_query=keyword or place_type or cfg.get("label", "anchor"),
            )
            if candidate.distance_miles is None:
                continue
            if candidate.distance_miles > max_distance_miles:
                continue
            pool.append((candidate.distance_miles, cfg.get("label", ""), candidate))

    if pool:
        pool.sort(key=lambda t: t[0])
        commercial_pool: list = []
        non_commercial_pool: list = []
        rejected: list = []
        for dist, label, candidate in pool:
            viable, reason = is_anchor_viable(
                types=candidate.types,
                name=candidate.name,
                formatted_address=candidate.formatted_address,
            )
            if not viable:
                rejected.append((candidate.name, reason))
                continue
            is_commercial, _ = has_commercial_signal(
                types=candidate.types, name=candidate.name,
            )
            (commercial_pool if is_commercial else non_commercial_pool).append(
                (dist, label, candidate)
            )
        if rejected:
            logger.info(f"anchor: filtered {len(rejected)} non-viable "
                        f"candidate(s): {rejected[:3]}")
        # Prefer a commercial-looking anchor (storefront, plaza, training
        # center) over a viable-but-generic establishment ("Pinterest", a
        # random LLC). The downstream pipeline downgrades the candidate
        # display when only a non-commercial anchor exists.
        winning_pool = commercial_pool or non_commercial_pool
        if winning_pool:
            _, best_label, best = winning_pool[0]
            client.hydrate_with_details(best)
            pool_kind = "commercial" if commercial_pool else "non-commercial-fallback"
            logger.info(f"anchor selected: {best.name} "
                        f"({best_label}, {best.distance_miles} mi, "
                        f"{pool_kind}) from {len(winning_pool)} candidate(s)")
            return best

    fallback = _reverse_geocode(origin[0], origin[1])
    if fallback is not None:
        fallback.distance_miles = round(
            haversine_miles(origin, (fallback.latitude or origin[0],
                                     fallback.longitude or origin[1])), 3,
        )
        logger.info(f"anchor fallback (reverse-geocode): {fallback.name}")
    return fallback
