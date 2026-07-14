"""
Yelp Fusion API — CPR/BLS competitor augmentation.

Google Places gives us a competitor list, but Google's ``establishment``
and ``point_of_interest`` tags don't distinguish a CPR training center
from a generic medical office. Yelp's category taxonomy *does*
(``cprclasses``, ``firstaidclasses``, ``healthandmedical/training``).
This module pulls Yelp's view of the same competitor space and uses it
to:

1. Cross-validate Google's competitor pool — a place that shows up on
   both Yelp's CPR-classes category AND Google's CPR text search is a
   higher-confidence competitor than one that shows up on only Google.
2. Augment matched Google competitors with Yelp's rating, review count,
   Yelp URL, and clean category list.
3. Surface Yelp-only competitors that Google missed (rare but happens
   for niche training centers).

Feature-flagged on ``YELP_API_KEY``. Empty → no-op, the pipeline keeps
running on Google data alone.

Yelp Fusion API: https://docs.developer.yelp.com/reference/v3_business_search
Free tier: 500 requests / day for read-only Business endpoints.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


YELP_API_KEY: str = os.getenv("YELP_API_KEY", "")
YELP_BASE = "https://api.yelp.com/v3/businesses/search"
YELP_TTL_SECONDS = 14 * 86400  # competitor rosters drift slowly

# Yelp categories most aligned with CPR / first-aid training. Yelp uses
# slug-style aliases. Comma-joined sends an OR query.
YELP_COMPETITOR_CATEGORIES = "cprclasses,firstaidclasses"

# When matching a Yelp business to a Google competitor, candidates must be
# within this distance AND have a reasonably matching name (Jaccard >= 0.5
# over token sets, case-folded).
MATCH_DISTANCE_MILES = 0.05  # ~80 meters; two listings of the same business
MATCH_JACCARD_THRESHOLD = 0.5


def is_configured() -> bool:
    return bool(YELP_API_KEY)


def _yelp_request(
    latitude: float,
    longitude: float,
    radius_miles: float,
    limit: int = 50,
) -> List[Dict[str, object]]:
    """One Yelp businesses/search call. Returns the raw list or []."""
    if not is_configured():
        return []
    radius_m = int(round(min(radius_miles * 1609.34, 40_000)))  # Yelp caps at 40km
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius_m,
        "categories": YELP_COMPETITOR_CATEGORIES,
        "limit": min(limit, 50),  # API hard cap
        "sort_by": "distance",
    }
    headers = {
        "Authorization": f"Bearer {YELP_API_KEY}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            YELP_BASE, params=params, headers=headers, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning(f"yelp: request failed: {exc}")
        return []
    if resp.status_code >= 400:
        preview = (resp.text or "")[:200]
        if YELP_API_KEY in preview:
            preview = preview.replace(YELP_API_KEY, "<key>")
        logger.warning(f"yelp: HTTP {resp.status_code}: {preview!r}")
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("yelp: non-JSON body")
        return []
    return data.get("businesses") or []


def fetch_yelp_competitors(
    origin: Tuple[float, float],
    radius_miles: float = 5.0,
    cache: Optional[Cache] = None,
) -> List[Dict[str, object]]:
    """Fetch Yelp competitors near ``origin``.

    Returns a list of normalized dicts with: ``name``, ``yelp_id``,
    ``yelp_url``, ``yelp_rating``, ``yelp_review_count``,
    ``yelp_categories``, ``yelp_phone``, ``yelp_price``, ``latitude``,
    ``longitude``. Empty when Yelp isn't configured.
    """
    if not is_configured():
        return []
    lat, lon = origin
    cache_params = {
        "latitude": lat, "longitude": lon, "radius_miles": radius_miles,
        "categories": YELP_COMPETITOR_CATEGORIES,
    }

    def _live() -> List[Dict[str, object]]:
        raw = _yelp_request(lat, lon, radius_miles)
        return [_normalize_business(b) for b in raw if isinstance(b, dict)]

    value, _ = cached_call(
        cache, "yelp", "competitor_search", cache_params,
        ttl_seconds=YELP_TTL_SECONDS, live_call=_live,
    )
    return list(value or [])


def _normalize_business(raw: Dict[str, object]) -> Dict[str, object]:
    coords = raw.get("coordinates") or {}
    return {
        "name": raw.get("name") or "",
        "yelp_id": raw.get("id") or "",
        "yelp_url": raw.get("url") or "",
        "yelp_rating": raw.get("rating"),
        "yelp_review_count": raw.get("review_count"),
        "yelp_categories": [
            c.get("title") for c in (raw.get("categories") or [])
            if isinstance(c, dict) and c.get("title")
        ],
        "yelp_phone": raw.get("phone") or "",
        "yelp_price": raw.get("price") or "",
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
    }


def _name_jaccard(a: str, b: str) -> float:
    """Crude case-folded token-set Jaccard. ``""`` ↔ anything → 0."""
    ta = {t for t in (a or "").lower().split() if t}
    tb = {t for t in (b or "").lower().split() if t}
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


def _match_yelp_to_google(
    yelp_record: Dict[str, object],
    google_competitor: object,
) -> bool:
    """True when this Yelp business is the same place as ``google_competitor``."""
    g_lat = getattr(google_competitor, "latitude", None)
    g_lon = getattr(google_competitor, "longitude", None)
    y_lat = yelp_record.get("latitude")
    y_lon = yelp_record.get("longitude")
    if not all(isinstance(v, (int, float)) for v in (g_lat, g_lon, y_lat, y_lon)):
        return False
    if haversine_miles((g_lat, g_lon), (y_lat, y_lon)) > MATCH_DISTANCE_MILES:
        return False
    g_name = getattr(google_competitor, "name", "") or ""
    y_name = yelp_record.get("name") or ""
    return _name_jaccard(g_name, y_name) >= MATCH_JACCARD_THRESHOLD


def augment_competitors_with_yelp(
    google_competitors: List[object],
    yelp_records: List[Dict[str, object]],
) -> Dict[str, object]:
    """Cross-validate + augment a Google competitor list with Yelp data.

    Mutates each Google competitor PlaceProfile by stashing the matched
    Yelp fields under ``yelp_augmentation`` (a plain dict — keeps the
    PlaceProfile dataclass shape stable). Returns a summary dict for the
    competition_summary block.
    """
    matched = 0
    yelp_unmatched: List[Dict[str, object]] = []
    used_yelp_ids: set = set()
    yelp_rating_sum = 0.0
    yelp_rating_count = 0
    yelp_review_total = 0

    for comp in google_competitors:
        for yelp in yelp_records:
            yelp_id = yelp.get("yelp_id") or ""
            if yelp_id in used_yelp_ids:
                continue
            if _match_yelp_to_google(yelp, comp):
                used_yelp_ids.add(yelp_id)
                augmentation = {
                    "yelp_id": yelp_id,
                    "yelp_url": yelp.get("yelp_url") or "",
                    "yelp_rating": yelp.get("yelp_rating"),
                    "yelp_review_count": yelp.get("yelp_review_count"),
                    "yelp_categories": list(yelp.get("yelp_categories") or []),
                }
                try:
                    setattr(comp, "yelp_augmentation", augmentation)
                except Exception:
                    pass
                matched += 1
                if isinstance(yelp.get("yelp_rating"), (int, float)):
                    yelp_rating_sum += float(yelp["yelp_rating"])
                    yelp_rating_count += 1
                if isinstance(yelp.get("yelp_review_count"), (int, float)):
                    yelp_review_total += int(yelp["yelp_review_count"])
                break  # one Yelp record per Google competitor

    for yelp in yelp_records:
        if (yelp.get("yelp_id") or "") not in used_yelp_ids:
            yelp_unmatched.append(yelp)

    avg_yelp_rating = (yelp_rating_sum / yelp_rating_count
                       if yelp_rating_count else None)
    return {
        "yelp_matched_count": matched,
        "yelp_only_count": len(yelp_unmatched),
        "yelp_avg_rating": (round(avg_yelp_rating, 2)
                            if avg_yelp_rating is not None else None),
        "yelp_total_reviews": yelp_review_total or None,
        "yelp_unmatched_competitors": yelp_unmatched,
    }
