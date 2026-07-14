"""
Area profile enricher.

Top-level orchestrator that builds a complete per-candidate profile by
calling the anchor selector, demand, competition, and economy enrichers
and folding their output (plus provenance) into one dict ready for scoring
and reporting.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

from app.collectors.google_places import GooglePlacesClient
from app.collectors.job_postings import collect_job_posting_demand
from app.config import DISTANCE_BUCKETS_MILES, TRAINING_ECOSYSTEM_KEYS
from app.enrichers.accessibility import collect_accessibility_for_point
from app.enrichers.anchor import select_anchor
from app.enrichers.competition import collect_competition_for_point
from app.enrichers.demand_signals import collect_demand_for_point
from app.enrichers.economy import collect_economy_for_point
from app.models.place_profile import PlaceProfile
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import SourceTracker, utcnow_iso
from app.utils.viability_filter import has_commercial_signal, is_anchor_viable
from app.enrichers.anchor_status import (
    COMMERCIAL_PLAZA,
    INVALID_ANCHOR,
    VERIFIED_COMMERCIAL_SITE,
    AnchorAssessment,
    area_display_name,
    classify_anchor,
)

logger = get_logger(__name__)


def _new_candidate_id(city: str, state: str, index: int,
                      latitude: float, longitude: float) -> str:
    base = f"{state.upper()}-{city.lower().replace(' ', '_')}-{index:03d}"
    # Deterministic suffix: same physical location => same id across runs, so
    # scores, reports, and commercial_overrides.csv matches stay stable and the
    # scraper cache (keyed on rounded lat/lon) is reused instead of re-hit.
    # Coords rounded to ~11 m to match the cache key granularity.
    seed = f"{round(latitude, 4):.4f},{round(longitude, 4):.4f}"
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{suffix}"


def _sum_categories(counts_by_bucket: Dict[str, Dict[int, int]],
                    keys: List[str], bucket_mi: int) -> int:
    return sum(counts_by_bucket.get(k, {}).get(bucket_mi, 0) for k in keys)


def build_area_profile(
    client: GooglePlacesClient,
    city: str,
    state: str,
    latitude: float,
    longitude: float,
    radius_miles: float,
    candidate_index: int = 0,
    candidate_name: str = "",
    candidate_source: str = "city_center_grid",
    analyze_competitor_websites: Optional[bool] = None,
    analyze_reviews: bool = False,
) -> Dict[str, object]:
    """
    Assemble the full profile for one candidate location.

    Returns a dict containing:
      - identifying fields (candidate_id, city, state, lat, lon, name)
      - anchor: PlaceProfile of the nearest meaningful anchor (or None)
      - counts_by_bucket / counts_5mi
      - demand.places_by_category and demand.top_places (PlaceProfile lists)
      - competition (summary + competitors list as PlaceProfile)
      - economy block
      - sources / missing_fields (SourceTracker payload)
    """
    origin = (latitude, longitude)
    cid = _new_candidate_id(city, state, candidate_index, latitude, longitude)
    tracker = SourceTracker()

    logger.info(f"profile {cid}: ({latitude:.4f},{longitude:.4f}) "
                f"r={radius_miles}mi — selecting anchor")
    anchor: Optional[PlaceProfile] = select_anchor(client, origin)
    if anchor is not None:
        tracker.add(
            name=anchor.source_api or "Google Places API",
            url="https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            fields=["candidate_anchor"],
            notes=f"anchor: {anchor.name}",
        )

    logger.info(f"profile {cid}: collecting demand")
    demand = collect_demand_for_point(client, origin, radius_miles)
    for s in demand["sources"]:
        tracker.add(s["name"], s["url"], s["fields"], s.get("notes", ""))

    logger.info(f"profile {cid}: collecting competition")
    competition = collect_competition_for_point(
        client,
        origin,
        radius_miles,
        analyze_websites=analyze_competitor_websites,
        analyze_reviews=analyze_reviews,
    )
    for s in competition["sources"]:
        tracker.add(s["name"], s["url"], s["fields"], s.get("notes", ""))

    logger.info(f"profile {cid}: collecting accessibility proxies")
    accessibility = collect_accessibility_for_point(
        client,
        origin,
        radius_miles,
        counts_by_bucket=demand["counts_by_bucket"],
    )
    for s in accessibility["sources"]:
        tracker.add(s["name"], s["url"], s["fields"], s.get("notes", ""))

    logger.info(f"profile {cid}: collecting economy (Census + stubs)")
    economy = collect_economy_for_point(latitude, longitude, city=city, state=state)
    for block in (economy["census"], economy["labor"], economy["real_estate"]):
        for s in block["sources"]:
            tracker.add(s["name"], s["url"], s["fields"], s.get("notes", ""))

    for key, val in economy["census"]["values"].items():
        if val is None:
            tracker.mark_missing(f"census.{key}")
    for key, val in economy["labor"]["values"].items():
        if val is None:
            tracker.mark_missing(f"labor.{key}")
    for key, val in economy["real_estate"]["values"].items():
        if val is None:
            tracker.mark_missing(f"real_estate.{key}")

    logger.info(f"profile {cid}: collecting job-posting certification demand")
    job_demand = collect_job_posting_demand(city, state, latitude, longitude)
    for s in job_demand["sources"]:
        tracker.add(s["name"], s["url"], s["fields"], s.get("notes", ""))
    job_values = job_demand.get("values") or {}
    for key in ("active_postings_count", "certification_postings_count"):
        if job_values.get(key) is None:
            tracker.mark_missing(f"job_demand.{key}")

    counts_by_bucket: Dict[str, Dict[int, int]] = demand["counts_by_bucket"]
    counts_5mi: Dict[str, int] = {
        k: counts_by_bucket.get(k, {}).get(5, 0) for k in counts_by_bucket
    }
    training_ecosystem_5mi = _sum_categories(
        counts_by_bucket, list(TRAINING_ECOSYSTEM_KEYS), 5,
    )

    top_drivers = sorted(
        ((k, v) for k, v in counts_5mi.items() if v > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:5]

    if anchor is not None:
        viable, reason = is_anchor_viable(
            types=anchor.types,
            name=anchor.name,
            formatted_address=anchor.formatted_address,
        )
        is_commercial, commercial_reason = has_commercial_signal(
            types=anchor.types, name=anchor.name,
        )
    else:
        # No anchor resolved is unknown, not non-viable — don't auto-drop.
        viable, reason = True, ""
        is_commercial, commercial_reason = False, "no anchor resolved"

    needs_validation = bool(viable) and not is_commercial
    viability: Dict[str, object] = {
        "viable": bool(viable),
        "reason": reason,
        "commercial_anchor": bool(is_commercial),
        "commercial_reason": commercial_reason,
        "needs_validation": needs_validation,
    }

    # Anchor status: grade the anchor by what kind of place it is (verified
    # commercial site / plaza / area proxy / invalid). Labeling only — it never
    # unlocks site_score and changes no scoring math.
    base_profile = {
        "city": city, "state": state,
        "anchor": anchor.to_dict() if anchor else None,
    }
    if anchor is not None:
        assessment = classify_anchor(
            types=anchor.types, name=anchor.name,
            formatted_address=anchor.formatted_address,
        )
    else:
        assessment = AnchorAssessment(
            anchor_status=INVALID_ANCHOR,
            anchor_status_label="Invalid anchor",
            anchor_quality_score=0,
            anchor_display_name="",
            site_score_withheld=True,
            reason="no anchor resolved",
        )
    area_name = area_display_name(base_profile)

    # Display name: PREFER the area/corridor, never a random POI. A real
    # commercial site may lead with the anchor; an area-proxy / invalid anchor
    # must lead with the area so a landmark never looks like a lease-ready site.
    display_name = candidate_name or f"{city} candidate #{candidate_index}"
    if assessment.anchor_status in (VERIFIED_COMMERCIAL_SITE, COMMERCIAL_PLAZA) \
            and anchor and anchor.name:
        display_name = f"{anchor.name} — {city}"
    elif area_name:
        display_name = area_name

    profile: Dict[str, object] = {
        "candidate_id": cid,
        "city": city,
        "state": state,
        "latitude": latitude,
        "longitude": longitude,
        "candidate_name": display_name,
        "candidate_source": candidate_source,
        "radius_miles": radius_miles,
        "collected_at": utcnow_iso(),

        "anchor": anchor.to_dict() if anchor else None,
        "anchor_obj": anchor,  # raw PlaceProfile (stripped before JSON)
        "viability": viability,
        "anchor_status": assessment.to_dict(),
        "area_display_name": area_name,

        "counts_by_bucket": counts_by_bucket,
        "counts_5mi": counts_5mi,
        "saturated_demand_categories": list(
            demand.get("saturated_categories") or []
        ),
        "training_ecosystem_count_5mi": training_ecosystem_5mi,
        "top_demand_drivers": top_drivers,

        # NEW: top PlaceProfile lists per category (sorted by distance).
        "demand_top_places": {
            k: [p.to_dict() for p in lst]
            for k, lst in demand["top_places"].items()
        },
        "demand_top_places_obj": demand["top_places"],

        "competition_summary": competition["summary"],
        "competitors": [c.to_dict() for c in competition["competitors"]],
        "competitors_obj": competition["competitors"],
        "competitors_sample": [c.to_dict() for c in competition["competitors"][:10]],

        "accessibility": accessibility,

        "economy": economy,
        "job_demand": job_demand,

        "sources": [s.as_dict() for s in tracker.sources],
        "source_urls": tracker.source_urls,
        "source_names": tracker.source_names,
        "missing_fields": sorted(tracker.missing_fields),
        "distance_buckets_mi": list(DISTANCE_BUCKETS_MILES),
    }
    return profile
