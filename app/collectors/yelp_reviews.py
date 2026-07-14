"""
Yelp review-excerpt collector.

The Yelp Fusion reviews endpoint returns up to 3 review excerpts per business
on the free tier — short, but enough to detect *recurring* complaint themes
when aggregated across many competitors in a market. We only fetch reviews
for competitors Yelp already matched (we have their ``yelp_id`` from the
augmentation step), so no extra search calls are wasted.

Feature-flagged on ``YELP_API_KEY``. Cached 14 days. Each fetch is one call
per business; the caller caps how many competitors to pull.

Yelp reviews docs:
https://docs.developer.yelp.com/reference/v3_business_reviews
"""
from __future__ import annotations

from typing import Dict, List, Optional

import requests

from app.collectors.yelp_competitors import YELP_API_KEY, is_configured
from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

YELP_REVIEWS_URL = "https://api.yelp.com/v3/businesses/{id}/reviews"
YELP_REVIEWS_TTL_SECONDS = 14 * 86400


def fetch_reviews(
    yelp_id: str,
    cache: Optional[Cache] = None,
) -> List[Dict[str, object]]:
    """Return up to 3 review excerpts for a Yelp business.

    Each item: ``{"text": str, "rating": int, "time_created": str}``.
    Empty when Yelp isn't configured, the id is blank, or the call fails.
    """
    if not is_configured() or not yelp_id:
        return []

    def _live() -> List[Dict[str, object]]:
        return _live_reviews(yelp_id)

    value, _ = cached_call(
        cache, "yelp", "business_reviews", {"yelp_id": yelp_id},
        ttl_seconds=YELP_REVIEWS_TTL_SECONDS, live_call=_live,
    )
    return list(value or [])


def _live_reviews(yelp_id: str) -> List[Dict[str, object]]:
    url = YELP_REVIEWS_URL.format(id=yelp_id)
    headers = {
        "Authorization": f"Bearer {YELP_API_KEY}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers,
                            params={"limit": 3, "sort_by": "yelp_sort"},
                            timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning(f"yelp_reviews: request failed: {exc}")
        return []
    if resp.status_code >= 400:
        preview = (resp.text or "")[:160]
        if YELP_API_KEY in preview:
            preview = preview.replace(YELP_API_KEY, "<key>")
        logger.warning(f"yelp_reviews: HTTP {resp.status_code}: {preview!r}")
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    out: List[Dict[str, object]] = []
    for rv in data.get("reviews") or []:
        if not isinstance(rv, dict):
            continue
        out.append({
            "text": rv.get("text") or "",
            "rating": rv.get("rating"),
            "time_created": rv.get("time_created") or "",
        })
    return out
