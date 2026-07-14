"""
Foursquare Places API collector — clean commercial-anchor source.

Foursquare's place taxonomy distinguishes ``Shopping Mall``, ``Strip Mall``,
``Office Building``, ``Coworking Space``, and ``Medical Center`` cleanly —
where Google's legacy types collapse all of those into ``establishment +
point_of_interest``. That's why "Pinterest" and "Shahid kali" became top
anchors in the SF report: Google's type tags were too coarse to filter
them out at the anchor-selection stage.

This collector is feature-flagged via ``FOURSQUARE_API_KEY``. When the key
is missing the module is a no-op, the rest of the pipeline runs unchanged,
and anchor selection falls back to Google-only (the current behavior).
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT
from app.models.place_profile import PlaceProfile
from app.utils.cache import Cache, cached_call
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)


FOURSQUARE_API_KEY: str = os.getenv("FOURSQUARE_API_KEY", "")
# Foursquare migrated v3 in mid-2024: the api.foursquare.com/v3 endpoint
# now returns HTTP 410 "no longer supported". Use places-api.foursquare.com
# with Bearer auth + the X-Places-Api-Version header.
FOURSQUARE_BASE = "https://places-api.foursquare.com/places/search"
FOURSQUARE_API_VERSION = "2025-06-17"
FOURSQUARE_TTL_SECONDS = 30 * 86400  # category placements are stable

# Foursquare's current 24-char hex category IDs (queried live from their
# categories endpoint). These map to leasable commercial-space intent for
# ALLCPR's site-selection use case.
COMMERCIAL_CATEGORY_IDS = (
    "4bf58dd8d48988d1fd941735",  # Shopping Mall
    "5744ccdfe4b0c0459246b4dc",  # Shopping Plaza
    "63be6904847c3692a84b9b76",  # Office Building
    "4bf58dd8d48988d174941735",  # Coworking Space
    "4bf58dd8d48988d104941735",  # Medical Center
    "4bf58dd8d48988d177941735",  # Doctor's Office
    "4bf58dd8d48988d196941735",  # Hospital
    "52f2ab2ebcbc57f1066b8b46",  # Supermarket
    "4bf58dd8d48988d118951735",  # Grocery Store
    "5744ccdfe4b0c0459246b4af",  # Physical Therapy Clinic
)


def is_configured() -> bool:
    return bool(FOURSQUARE_API_KEY)


def search_commercial_anchors(
    origin: Tuple[float, float],
    radius_miles: float = 0.75,
    limit: int = 10,
    cache: Optional[Cache] = None,
) -> List[PlaceProfile]:
    """Return Foursquare commercial anchors near ``origin``.

    Returns an empty list when ``FOURSQUARE_API_KEY`` is not configured.
    Caller should treat that as a clean signal to fall back to Google.
    """
    if not FOURSQUARE_API_KEY:
        return []
    lat, lon = origin
    radius_m = int(round(max(50.0, radius_miles * 1609.34)))
    radius_m = min(radius_m, 100_000)  # FSQ caps radius at 100km
    params = {
        "ll": f"{lat},{lon}",
        "radius": radius_m,
        "fsq_category_ids": ",".join(COMMERCIAL_CATEGORY_IDS),
        "limit": limit,
        "sort": "DISTANCE",
    }

    def _live() -> List[dict]:
        return _live_search(params)

    raw, _ = cached_call(
        cache,
        provider="foursquare",
        method="places_search",
        params=params,
        ttl_seconds=FOURSQUARE_TTL_SECONDS,
        live_call=_live,
    )
    if not raw:
        return []
    return [
        _to_place_profile(item, origin=origin)
        for item in raw
        if isinstance(item, dict)
    ]


def _live_search(params: dict) -> List[dict]:
    # Migrated to the post-2024 Foursquare auth: Bearer prefix + dated
    # X-Places-Api-Version header. The old api.foursquare.com/v3 endpoint
    # returns HTTP 410 on the legacy auth shape.
    headers = {
        "Authorization": f"Bearer {FOURSQUARE_API_KEY}",
        "X-Places-Api-Version": FOURSQUARE_API_VERSION,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            FOURSQUARE_BASE, params=params, headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning(f"foursquare: request failed: {exc}")
        return []
    if resp.status_code >= 400:
        preview = (resp.text or "")[:200]
        if FOURSQUARE_API_KEY and FOURSQUARE_API_KEY in preview:
            preview = preview.replace(FOURSQUARE_API_KEY, "<key>")
        logger.warning(f"foursquare: HTTP {resp.status_code}: {preview!r}")
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("foursquare: non-JSON body")
        return []
    return data.get("results") or []


def _to_place_profile(
    raw: dict,
    origin: Optional[Tuple[float, float]] = None,
) -> PlaceProfile:
    """Map a Foursquare ``places/search`` result to a PlaceProfile.

    Note: PlaceProfile is Google-Places-shaped, so we map fields to the
    closest equivalents. ``fsq_place_id`` lives in the ``place_id`` slot
    (prefixed so collisions with Google IDs are impossible). Response
    shape is the post-2024 places-api.foursquare.com format: lat/lon at
    top level, no nested ``geocodes`` block.
    """
    lat = raw.get("latitude")
    lon = raw.get("longitude")
    distance_m = raw.get("distance")
    if isinstance(distance_m, (int, float)):
        distance_miles: Optional[float] = round(distance_m / 1609.34, 3)
    elif (origin is not None and lat is not None and lon is not None):
        distance_miles = round(haversine_miles(origin, (lat, lon)), 3)
    else:
        distance_miles = None

    location = raw.get("location") or {}
    address_parts = [
        location.get("address"),
        location.get("locality"),
        location.get("region"),
        location.get("postcode"),
    ]
    formatted_address = ", ".join(p for p in address_parts if p)

    # Foursquare categories are richer than Google types; flatten the primary
    # category name into PlaceProfile.types so downstream filters can match.
    category_names = []
    for cat in raw.get("categories") or []:
        if isinstance(cat, dict) and cat.get("name"):
            category_names.append(cat["name"])

    fsq_id = raw.get("fsq_place_id") or raw.get("fsq_id") or ""
    profile = PlaceProfile(
        place_id=f"fsq:{fsq_id}",
        name=raw.get("name") or "",
        formatted_address=formatted_address,
        latitude=lat,
        longitude=lon,
        category="anchor:foursquare",
        source_query="foursquare_commercial",
        google_maps_url="",
        website=raw.get("website") or "",
        phone_number=raw.get("tel") or "",
        rating=None,
        user_ratings_total=None,
        business_status="",
        types=category_names,
        distance_miles=distance_miles,
        photos=[],
        source_api="Foursquare Places API",
        collected_at=utcnow_iso(),
        confidence="api",
    )
    return profile
