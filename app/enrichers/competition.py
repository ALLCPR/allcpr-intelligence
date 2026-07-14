"""
Competition enricher.

Wraps the competitors collector and produces summary stats useful to scoring
(average rating, total reviews, count by distance bucket, weakness signals)
plus a list of PlaceProfile objects for the report.
"""
from __future__ import annotations

from statistics import mean
from typing import Dict, List, Optional, Tuple

from app.collectors import yelp_competitors as _yelp
from app.collectors import yelp_reviews as _yelp_reviews
from app.collectors.website_analysis import analyze_website
from app.collectors.competitors import collect_competitors
from app.collectors.google_places import GooglePlacesClient
from app.enrichers.review_sentiment import analyze_reviews as _analyze_reviews
from app.config import (
    COMPETITOR_WEBSITE_ANALYSIS_ENABLED,
    COMPETITOR_WEBSITE_TIMEOUT,
    DISTANCE_BUCKETS_MILES,
)
from app.models.place_profile import PlaceProfile
from app.utils.geo_utils import bucket_distances
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)


def _safe_mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else None


def collect_competition_for_point(
    client: GooglePlacesClient,
    origin: Tuple[float, float],
    radius_miles: float,
    analyze_websites: Optional[bool] = None,
    analyze_reviews: bool = False,
    reviews_top_n: int = 8,
) -> Dict[str, object]:
    competitors: List[PlaceProfile] = collect_competitors(client, origin, radius_miles)

    should_analyze = (
        COMPETITOR_WEBSITE_ANALYSIS_ENABLED
        if analyze_websites is None
        else analyze_websites
    )
    website_sources: List[Dict[str, object]] = []
    cache = getattr(client, "_cache", None)
    if should_analyze:
        for comp in competitors:
            analysis = analyze_website(
                comp.website,
                timeout=COMPETITOR_WEBSITE_TIMEOUT,
                cache=cache,
            )
            comp.website_analysis = analysis
            if analysis.get("checked"):
                fields = [f"competitor_website.{sig}" for sig in (
                    list(analysis.get("detected") or [])
                    + list(analysis.get("missing") or [])
                )]
                website_sources.append({
                    "name": "Competitor website homepage fetch",
                    "url": comp.website,
                    "fields": fields,
                    "collected_at": analysis.get("retrieved_at") or utcnow_iso(),
                    "notes": f"{comp.name}: checked homepage"
                             + (" + one obvious page"
                                if len(analysis.get("pages_checked") or []) > 1
                                else ""),
                })

    distances = [c.distance_miles for c in competitors
                 if c.distance_miles is not None]
    bucket_counts = bucket_distances(distances, DISTANCE_BUCKETS_MILES)

    ratings = [float(c.rating) for c in competitors if c.rating is not None]
    reviews = [int(c.user_ratings_total) for c in competitors
               if c.user_ratings_total is not None]

    no_website = sum(1 for c in competitors if not c.website)
    no_phone = sum(1 for c in competitors if not c.phone_number)
    checked = sum(1 for c in competitors if (c.website_analysis or {}).get("checked"))
    online_booking_missing = sum(
        1 for c in competitors
        if "online_booking" in ((c.website_analysis or {}).get("missing") or [])
    )
    schedule_missing = sum(
        1 for c in competitors
        if "class_schedule" in ((c.website_analysis or {}).get("missing") or [])
    )
    pricing_missing = sum(
        1 for c in competitors
        if "pricing" in ((c.website_analysis or {}).get("missing") or [])
    )
    contact_friction = sum(
        1 for c in competitors
        if "contact_friction" in ((c.website_analysis or {}).get("detected") or [])
    )
    outdated = sum(
        1 for c in competitors
        if "outdated_website" in ((c.website_analysis or {}).get("detected") or [])
    )
    acls_pals_offered = sum(
        1 for c in competitors
        if "acls_pals_offered" in ((c.website_analysis or {}).get("detected") or [])
    )
    acls_pals_missing = sum(
        1 for c in competitors
        if "acls_pals_offered" in ((c.website_analysis or {}).get("missing") or [])
    )
    group_corporate_offered = sum(
        1 for c in competitors
        if "group_corporate_offered" in ((c.website_analysis or {}).get("detected") or [])
    )
    group_corporate_missing = sum(
        1 for c in competitors
        if "group_corporate_offered" in ((c.website_analysis or {}).get("missing") or [])
    )
    weekend_offered = sum(
        1 for c in competitors
        if "weekend_classes_offered" in ((c.website_analysis or {}).get("detected") or [])
    )
    weekend_missing = sum(
        1 for c in competitors
        if "weekend_classes_offered" in ((c.website_analysis or {}).get("missing") or [])
    )
    multilingual_offered = sum(
        1 for c in competitors
        if "multilingual_support" in ((c.website_analysis or {}).get("detected") or [])
    )
    multilingual_missing = sum(
        1 for c in competitors
        if "multilingual_support" in ((c.website_analysis or {}).get("missing") or [])
    )

    # Per-competitor scale bands proxy (review counts as throughput proxy).
    scale_large = sum(
        1 for c in competitors
        if (c.user_ratings_total or 0) >= 100
    )
    scale_medium = sum(
        1 for c in competitors
        if 25 <= (c.user_ratings_total or 0) < 100
    )
    scale_small = sum(
        1 for c in competitors
        if 0 < (c.user_ratings_total or 0) < 25
    )
    scale_unknown = sum(
        1 for c in competitors
        if not c.user_ratings_total
    )
    top_competitor_reviews = max(
        ((c.user_ratings_total or 0) for c in competitors),
        default=0,
    )

    summary: Dict[str, object] = {
        "competitor_count_total": len(competitors),
        "competitor_count_by_bucket_mi": bucket_counts,
        "competitor_avg_rating": _safe_mean(ratings),
        "competitor_total_reviews": sum(reviews) if reviews else 0,
        "competitor_with_rating": len(ratings),
        "competitor_no_website": no_website,
        "competitor_no_phone": no_phone,
        "competitor_low_rating_count": sum(1 for r in ratings if r < 4.0),
        "website_analysis_checked_count": checked,
        "competitor_online_booking_missing": online_booking_missing,
        "competitor_class_schedule_missing": schedule_missing,
        "competitor_pricing_missing": pricing_missing,
        "competitor_contact_friction_detected": contact_friction,
        "competitor_outdated_website_detected": outdated,
        "competitor_acls_pals_offered": acls_pals_offered,
        "competitor_acls_pals_missing": acls_pals_missing,
        "competitor_group_corporate_offered": group_corporate_offered,
        "competitor_group_corporate_missing": group_corporate_missing,
        "competitor_weekend_offered": weekend_offered,
        "competitor_weekend_missing": weekend_missing,
        "competitor_multilingual_offered": multilingual_offered,
        "competitor_multilingual_missing": multilingual_missing,
        "competitor_scale_large": scale_large,
        "competitor_scale_medium": scale_medium,
        "competitor_scale_small": scale_small,
        "competitor_scale_unknown": scale_unknown,
        "competitor_top_reviews": top_competitor_reviews,
    }

    # Yelp competitor augmentation — cross-validate + enrich with Yelp's
    # cleaner CPR/firstaid taxonomy. Feature-flagged on YELP_API_KEY.
    yelp_summary: Dict[str, object] = {}
    if _yelp.is_configured() and competitors:
        cache = getattr(client, "_cache", None)
        yelp_records = _yelp.fetch_yelp_competitors(
            origin=origin, radius_miles=radius_miles, cache=cache,
        )
        if yelp_records:
            yelp_summary = _yelp.augment_competitors_with_yelp(
                competitors, yelp_records,
            )
            # Yelp-only competitors are real competitors Google missed.
            summary_matched = int(yelp_summary.get("yelp_matched_count") or 0)
            summary_only = int(yelp_summary.get("yelp_only_count") or 0)
            logger.info(
                f"yelp: matched {summary_matched} Google competitor(s); "
                f"{summary_only} Yelp-only competitor(s) added"
            )

    if yelp_summary:
        summary["yelp_matched_count"] = yelp_summary.get("yelp_matched_count")
        summary["yelp_only_count"] = yelp_summary.get("yelp_only_count")
        summary["yelp_avg_rating"] = yelp_summary.get("yelp_avg_rating")
        summary["yelp_total_reviews"] = yelp_summary.get("yelp_total_reviews")
        # If Yelp surfaced new competitors, expose them in the count totals.
        only_count = int(yelp_summary.get("yelp_only_count") or 0)
        if only_count > 0:
            summary["competitor_count_total"] = (
                int(summary.get("competitor_count_total") or 0) + only_count
            )

    # Review complaint-theme analysis — WHY competitors are rated poorly.
    # Primary source is Google Place Details reviews (already on the
    # hydrated competitors, same Atmosphere SKU as their ratings). Falls
    # back to Yelp's reviews endpoint, which is paywalled on most tiers.
    review_frustrations = None
    if analyze_reviews:
        cache = getattr(client, "_cache", None)
        all_reviews: List[Dict[str, object]] = []
        review_source = ""
        # 1) Google reviews from hydrated competitors (closest first).
        for comp in competitors[:reviews_top_n]:
            for rv in (comp.reviews or []):
                all_reviews.append(rv)
        if all_reviews:
            review_source = "Google Place Details"
        # 2) Fallback: Yelp reviews for matched competitors (if available).
        elif _yelp.is_configured():
            yelp_ids: List[str] = []
            for comp in competitors:
                aug = getattr(comp, "yelp_augmentation", None) or {}
                yid = aug.get("yelp_id")
                if yid:
                    yelp_ids.append(yid)
                if len(yelp_ids) >= reviews_top_n:
                    break
            for yid in yelp_ids:
                all_reviews.extend(_yelp_reviews.fetch_reviews(yid, cache=cache))
            if all_reviews:
                review_source = "Yelp Fusion reviews"
        if all_reviews:
            review_frustrations = _analyze_reviews(all_reviews)
            summary["review_complaint_themes"] = review_frustrations.theme_counts
            summary["top_market_frustrations"] = \
                review_frustrations.top_frustrations
            summary["reviews_scanned"] = review_frustrations.reviews_scanned
            summary["review_data_confidence"] = \
                review_frustrations.data_confidence
            summary["review_source"] = review_source
            logger.info(
                f"reviews: scanned {review_frustrations.reviews_scanned} "
                f"excerpt(s) from {review_source}; top theme(s) "
                f"{[f['theme'] for f in review_frustrations.top_frustrations[:3]]}"
            )

    sources = [{
        "name": "Google Places API (Text Search)",
        "url": "https://maps.googleapis.com/maps/api/place/textsearch/json",
        "fields": ["competitor_count_total", "competitor_avg_rating",
                   "competitor_total_reviews"],
        "collected_at": utcnow_iso(),
        "notes": f"radius={radius_miles}mi",
    }]
    if yelp_summary:
        sources.append({
            "name": "Yelp Fusion API (CPR/first-aid classes)",
            "url": "https://api.yelp.com/v3/businesses/search",
            "fields": ["yelp_matched_count", "yelp_only_count",
                       "yelp_avg_rating", "yelp_total_reviews"],
            "collected_at": utcnow_iso(),
            "notes": (
                f"matched {yelp_summary.get('yelp_matched_count')} Google "
                f"competitor(s); {yelp_summary.get('yelp_only_count')} "
                f"Yelp-only adds"
            ),
        })
    if review_frustrations is not None:
        sources.append({
            "name": "Yelp Fusion API (review excerpts)",
            "url": "https://api.yelp.com/v3/businesses/{id}/reviews",
            "fields": ["top_market_frustrations", "review_complaint_themes"],
            "collected_at": utcnow_iso(),
            "notes": (
                f"{review_frustrations.reviews_scanned} review excerpt(s) "
                f"scanned for complaint themes (negative reviews only)"
            ),
        })
    sources.extend(website_sources)

    return {
        "competitors": competitors,
        "summary": summary,
        "sources": sources,
    }
