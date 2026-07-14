"""
Google Places API collector.

Wraps Nearby Search, Text Search, and Place Details with retry, rate limiting,
and a consistent normalized output shape. Returns `PlaceProfile` objects.
Pure data fetching — no scoring or interpretation lives here.

This client targets Places API (New) and maps v1 Place responses back into the
legacy dict shape consumed by `PlaceProfile.from_google_places`.

Strategy note: Places is currently finalist context/display enrichment, not the
default national scoring engine. A prior live scoring test moved overall
correlation only 0.103 → 0.105 and saturated dense ZIPs at the nearby-search
result cap, so any score use must stay behind explicit backtest-gated flags.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import (
    GOOGLE_MAPS_API_KEY,
    MAX_RETRIES,
    RATE_LIMIT_SECONDS,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF_SECONDS,
    ttl_for,
)
from app.models.place_profile import PlaceProfile
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

PLACES_V1_BASE_URL = "https://places.googleapis.com/v1"
NEARBY_URL = f"{PLACES_V1_BASE_URL}/places:searchNearby"
TEXT_URL = f"{PLACES_V1_BASE_URL}/places:searchText"
DETAILS_URL = f"{PLACES_V1_BASE_URL}/places"
GOOGLE_PLACES_API_FLAVOR = "places_api_new"

_PLACE_FIELDS = [
    "id", "displayName", "formattedAddress", "shortFormattedAddress",
    "location", "rating", "userRatingCount", "types", "googleMapsUri",
    "websiteUri", "businessStatus", "internationalPhoneNumber",
    "nationalPhoneNumber", "regularOpeningHours", "priceLevel", "photos",
]
SEARCH_FIELD_MASK = ",".join(f"places.{field}" for field in _PLACE_FIELDS)
DETAIL_FIELD_MASK = ",".join(_PLACE_FIELDS + ["reviews"])


class GooglePlacesError(RuntimeError):
    pass


class GooglePlacesClient:
    """Thin, retry-aware Google Places client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        rate_limit_seconds: float = RATE_LIMIT_SECONDS,
        max_retries: int = MAX_RETRIES,
        cache: Optional[Cache] = None,
    ) -> None:
        self.api_key = api_key or GOOGLE_MAPS_API_KEY
        if not self.api_key:
            raise GooglePlacesError(
                "Missing GOOGLE_MAPS_API_KEY. Add it to your .env file."
            )
        self.rate_limit = max(rate_limit_seconds, 0.0)
        self.max_retries = max(max_retries, 1)
        self.session = requests.Session()
        self._last_request_at = 0.0
        self._cache = cache

    # ------------------------------------------------------------------ #
    # Low-level HTTP
    # ------------------------------------------------------------------ #

    def _sleep_for_rate_limit(self) -> None:
        if self.rate_limit <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        field_mask: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._sleep_for_rate_limit()
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                self._last_request_at = time.monotonic()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(f"Google Places transient error "
                               f"(attempt {attempt}/{self.max_retries}): {exc}. "
                               f"Retrying in {wait:.1f}s.")
                time.sleep(wait)
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                if 500 <= resp.status_code < 600 and attempt < self.max_retries:
                    wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(f"Google Places HTTP {resp.status_code} "
                                   f"with invalid JSON "
                                   f"(attempt {attempt}/{self.max_retries}). "
                                   f"Retrying in {wait:.1f}s.")
                    time.sleep(wait)
                    continue
                raise GooglePlacesError(f"Invalid Google Places JSON: {exc}") from exc

            if resp.status_code >= 400:
                err = data.get("error") if isinstance(data, dict) else {}
                message = (err or {}).get("message") or resp.text[:300]
                if 500 <= resp.status_code < 600 and attempt < self.max_retries:
                    wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(f"Google Places HTTP {resp.status_code} "
                                   f"(attempt {attempt}/{self.max_retries}). "
                                   f"Retrying in {wait:.1f}s.")
                    time.sleep(wait)
                    continue
                raise GooglePlacesError(
                    f"HTTP {resp.status_code}: {message}"
                )

            # Legacy-style API errors are kept here for tests/cached oddities;
            # Places API (New) normally reports errors as HTTP JSON bodies.
            status = data.get("status", "")
            if not status or status in ("OK", "ZERO_RESULTS"):
                return data
            if status == "OVER_QUERY_LIMIT" and attempt < self.max_retries:
                wait = RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(f"Google Places OVER_QUERY_LIMIT; "
                               f"sleeping {wait:.1f}s before retry.")
                time.sleep(wait)
                continue
            raise GooglePlacesError(
                f"Google Places API status={status}: "
                f"{data.get('error_message', '')}"
            )

        raise GooglePlacesError(
            f"Google Places request failed after {self.max_retries} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------ #
    # Search wrappers
    # ------------------------------------------------------------------ #

    def nearby_search(
        self,
        location: Tuple[float, float],
        radius_meters: int,
        place_type: Optional[str] = None,
        keyword: Optional[str] = None,
        max_pages: int = 2,
    ) -> List[Dict]:
        params = {
            "latitude": location[0], "longitude": location[1],
            "radius_meters": radius_meters,
            "place_type": place_type or "",
            "keyword": keyword or "",
            "max_pages": max_pages,
        }
        value, _ = cached_call(
            self._cache, "google_places", "nearby_search", params,
            ttl_seconds=ttl_for("google_places", "nearby_search"),
            live_call=lambda: self._nearby_search_live(
                location, radius_meters, place_type=place_type,
                keyword=keyword, max_pages=max_pages),
        )
        return value

    def _nearby_search_live(
        self,
        location: Tuple[float, float],
        radius_meters: int,
        place_type: Optional[str] = None,
        keyword: Optional[str] = None,
        max_pages: int = 2,
    ) -> List[Dict]:
        if keyword:
            return self._text_search_live(
                keyword,
                location=location,
                radius_meters=radius_meters,
                included_type=place_type,
                max_pages=max_pages,
            )

        body: Dict[str, Any] = {
            "maxResultCount": 20,
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": location[0],
                        "longitude": location[1],
                    },
                    "radius": float(radius_meters),
                }
            },
        }
        if place_type:
            body["includedTypes"] = [place_type]

        data = self._request_json(
            "POST", NEARBY_URL, field_mask=SEARCH_FIELD_MASK, json_body=body,
        )
        return [_places_v1_to_legacy(place) for place in data.get("places", [])]

    def text_search(self, query: str, location: Optional[Tuple[float, float]] = None,
                    radius_meters: Optional[int] = None, max_pages: int = 1) -> List[Dict]:
        params = {
            "query": query,
            "latitude": (location or (0, 0))[0],
            "longitude": (location or (0, 0))[1],
            "radius_meters": radius_meters or 0,
            "max_pages": max_pages,
        }
        value, _ = cached_call(
            self._cache, "google_places", "text_search", params,
            ttl_seconds=ttl_for("google_places", "text_search"),
            live_call=lambda: self._text_search_live(
                query, location=location, radius_meters=radius_meters,
                max_pages=max_pages),
        )
        return value

    def _text_search_live(
        self,
        query: str,
        location: Optional[Tuple[float, float]] = None,
        radius_meters: Optional[int] = None,
        max_pages: int = 1,
        included_type: Optional[str] = None,
    ) -> List[Dict]:
        results: List[Dict] = []
        token: Optional[str] = None
        for _ in range(max_pages):
            body: Dict[str, Any] = {"textQuery": query, "pageSize": 20}
            if token:
                body["pageToken"] = token
            elif location is not None and radius_meters is not None:
                body["locationBias"] = {
                    "circle": {
                        "center": {
                            "latitude": location[0],
                            "longitude": location[1],
                        },
                        "radius": float(radius_meters),
                    }
                }
            if included_type:
                body["includedType"] = included_type

            data = self._request_json(
                "POST", TEXT_URL, field_mask=SEARCH_FIELD_MASK, json_body=body,
            )
            results.extend(_places_v1_to_legacy(place)
                           for place in data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
            time.sleep(2.0)
        return results

    def place_details(self, place_id: str) -> Dict:
        params = {"place_id": place_id}
        value, _ = cached_call(
            self._cache, "google_places", "place_details", params,
            ttl_seconds=ttl_for("google_places", "place_details"),
            live_call=lambda: self._place_details_live(place_id),
        )
        return value

    def _place_details_live(self, place_id: str) -> Dict:
        if not place_id:
            return {}
        resource = place_id if place_id.startswith("places/") else f"places/{place_id}"
        data = self._request_json(
            "GET",
            f"{PLACES_V1_BASE_URL}/{resource}",
            field_mask=DETAIL_FIELD_MASK,
        )
        return _places_v1_to_legacy(data)

    def hydrate_with_details(self, profile: PlaceProfile) -> PlaceProfile:
        """Fetch Place Details and merge into the given profile in-place."""
        if not profile.place_id:
            return profile
        try:
            details = self.place_details(profile.place_id)
        except GooglePlacesError as exc:
            logger.warning(f"place_details failed for {profile.place_id}: {exc}")
            return profile
        profile.merge_details(details)
        return profile


# --------------------------------------------------------------------------- #
# Conversion helpers
# --------------------------------------------------------------------------- #

def _display_text(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("text", "") or ""
    if isinstance(value, str):
        return value
    return ""


def _price_level_to_legacy(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    mapping = {
        "PRICE_LEVEL_FREE": 0,
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
    }
    return mapping.get(str(value or ""))


def _photo_to_legacy(photo: Dict[str, Any]) -> Dict[str, Any]:
    attributions = []
    for attr in photo.get("authorAttributions") or []:
        if isinstance(attr, dict):
            display = attr.get("displayName") or attr.get("uri") or ""
            if display:
                attributions.append(display)
        elif attr:
            attributions.append(str(attr))
    return {
        "photo_reference": photo.get("name", "") or "",
        "width": photo.get("widthPx"),
        "height": photo.get("heightPx"),
        "html_attributions": attributions,
    }


def _review_to_legacy(review: Dict[str, Any]) -> Dict[str, Any]:
    text = review.get("text")
    return {
        "text": _display_text(text),
        "rating": review.get("rating"),
        "relative_time_description": (
            review.get("relativePublishTimeDescription")
            or review.get("publishTime")
            or ""
        ),
        "time": review.get("publishTime") or "",
    }


def _places_v1_to_legacy(place: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Places API (New) Place object into the legacy Web Service shape."""
    location = place.get("location") or {}
    opening_hours = place.get("regularOpeningHours") or {}
    legacy: Dict[str, Any] = {
        "place_id": place.get("id", "") or "",
        "name": _display_text(place.get("displayName")),
        "formatted_address": (
            place.get("formattedAddress")
            or place.get("shortFormattedAddress")
            or ""
        ),
        "geometry": {
            "location": {
                "lat": location.get("latitude"),
                "lng": location.get("longitude"),
            }
        },
        "rating": place.get("rating"),
        "user_ratings_total": place.get("userRatingCount"),
        "types": list(place.get("types") or []),
        "url": place.get("googleMapsUri", "") or "",
        "website": place.get("websiteUri", "") or "",
        "business_status": place.get("businessStatus", "") or "",
        "international_phone_number": place.get("internationalPhoneNumber", "") or "",
        "formatted_phone_number": place.get("nationalPhoneNumber", "") or "",
        "opening_hours": {
            "weekday_text": list(opening_hours.get("weekdayDescriptions") or []),
        },
        "price_level": _price_level_to_legacy(place.get("priceLevel")),
        "photos": [
            _photo_to_legacy(photo)
            for photo in place.get("photos") or []
            if isinstance(photo, dict) and photo.get("name")
        ],
        "reviews": [
            _review_to_legacy(review)
            for review in place.get("reviews") or []
            if isinstance(review, dict)
        ],
    }
    if legacy["geometry"]["location"]["lat"] is None:
        legacy["geometry"]["location"].pop("lat", None)
    if legacy["geometry"]["location"]["lng"] is None:
        legacy["geometry"]["location"].pop("lng", None)
    return legacy


def to_place_profile(
    raw: Dict,
    category: str,
    origin: Tuple[float, float],
    source_query: str,
) -> PlaceProfile:
    """Build a PlaceProfile from a raw search result, computing distance."""
    return PlaceProfile.from_google_places(
        raw, category=category, origin=origin, source_query=source_query,
    )


def miles_to_meters(miles: float) -> int:
    return int(math.ceil(miles * 1609.344))
