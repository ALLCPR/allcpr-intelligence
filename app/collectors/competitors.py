"""
CPR / training competitor collector.

Uses Google Places text search around a candidate location to find existing
CPR/BLS/First Aid/EMT training providers. Optionally hydrates the top-N
competitors with Place Details so the report can show phone, website, and
opening hours.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from app.collectors.google_places import (
    GooglePlacesClient,
    miles_to_meters,
    to_place_profile,
)
from app.config import COMPETITION_QUERIES, COMPETITOR_HYDRATE_TOP_N
from app.models.place_profile import PlaceProfile
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


def collect_competitors(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    radius_miles: float,
    queries: Optional[List[str]] = None,
    hydrate_top_n: int = COMPETITOR_HYDRATE_TOP_N,
) -> List[PlaceProfile]:
    """
    Search for CPR/BLS/First Aid training competitors near `origin`.
    Deduplicates by place_id. Each row is categorized as 'competitor'.

    The closest `hydrate_top_n` competitors get a follow-up Place Details
    call so phone/website/hours are populated for the report.
    """
    queries = queries or COMPETITION_QUERIES
    radius_m = miles_to_meters(radius_miles)
    seen: set[str] = set()
    profiles: List[PlaceProfile] = []
    for q in queries:
        logger.info(f"competition search: {q!r} around {origin} (r={radius_miles}mi)")
        try:
            raw = client.text_search(
                q, location=origin, radius_meters=radius_m, max_pages=1,
            )
        except Exception as exc:
            logger.warning(f"competitor query failed {q!r}: {exc}")
            continue
        for r in raw:
            pid = r.get("place_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            profiles.append(to_place_profile(
                r, category="competitor", origin=origin, source_query=q,
            ))

    profiles.sort(
        key=lambda p: (p.distance_miles if p.distance_miles is not None else 9999.0),
    )

    if hydrate_top_n > 0:
        for p in profiles[:hydrate_top_n]:
            client.hydrate_with_details(p)

    logger.info(f"competition: {len(profiles)} unique competitors found "
                f"(hydrated {min(hydrate_top_n, len(profiles))})")
    return profiles
