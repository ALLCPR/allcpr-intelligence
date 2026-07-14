"""
Live external instructor-lead sourcing — the last mile of the "source
externally" lane. Turns a target ZIP into real, named, contactable leads:
existing CPR / first-aid training businesses and providers near the area, whose
owners and staff are already credentialed instructors ALLCPR can recruit or
partner with.

It reuses the repo's existing, cached, key-gated collectors — Yelp
(``cprclasses,firstaidclasses``) and Google Places (CPR/BLS/AHA/ARC training) —
plus the competitor providers already scraped into ``competitor_classes.csv``.
No new scraping surface, no ToS-hostile targets (no LinkedIn/Indeed scraping —
Indeed is handled by the posting planner instead).

Honesty rule holds: a business is a *lead*, never a certified instructor, so
every result is ``SIGNAL_ONLY`` — a named, contactable place to source real
instructors from, whose credentials still get verified before anyone teaches.
Without API keys the live sources return nothing (the local competitor
providers still work); the payload says which sources were configured.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.collectors.yelp_competitors import (
    _name_jaccard,
    fetch_yelp_competitors,
)
from app.collectors.yelp_competitors import is_configured as yelp_configured
from app.config import CACHE_DB, CACHE_ENABLED, GOOGLE_MAPS_API_KEY
from app.ops.local_market import competitor_context
from app.scoring.zip_demand import load_zip_centroids
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Focused, instructor-oriented Google Places queries (kept small to bound
# per-request API cost; the collector caches by query).
GOOGLE_QUERIES = (
    "CPR training",
    "BLS certification class",
    "American Heart Association training center",
    "Red Cross CPR class",
)

# CPR/first-aid signal words → a lead that names them is a stronger recruit.
_STRONG_WORDS = ("cpr", "bls", "first aid", "aed", "life support", "aha",
                 "red cross", "heartsaver", "resuscitation", "acls", "pals")


def _make_cache():
    if not CACHE_ENABLED:
        return None
    try:
        from app.utils.cache import Cache  # noqa: PLC0415
        return Cache(CACHE_DB)
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.warning(f"live_sourcing: cache unavailable: {exc}")
        return None


def _strong_name(name: str) -> bool:
    low = str(name or "").lower()
    return any(w in low for w in _STRONG_WORDS)


def _is_allcpr(name: str) -> bool:
    """ALLCPR's own listings are not external leads — filter them out."""
    low = str(name or "").lower().replace(" ", "")
    return "allcpr" in low or "allofusshouldlearn" in low


def _lead(name: str, source: str, *, phone: str = "", url: str = "",
          address: str = "", distance_miles: Optional[float] = None,
          rating: Optional[float] = None, review_count: Optional[int] = None,
          categories: Optional[List[str]] = None) -> Dict[str, Any]:
    contactable = bool(phone or url)
    return {
        "name": name.strip(),
        "source": source,
        "lead_type": "CPR_BUSINESS_OWNER",
        # A business is a signal-level lead, never a certified instructor.
        "credential_status": "SIGNAL_ONLY",
        "phone": phone or "",
        "url": url or "",
        "address": address or "",
        "distance_miles": distance_miles,
        "rating": rating,
        "review_count": review_count,
        "categories": categories or [],
        "contactable": contactable,
        "strong_signal": _strong_name(name),
        "note": ("Existing CPR/first-aid business — contact to recruit their "
                 "instructors, partner, or acqui-hire; verify credentials "
                 "before teaching."),
    }


def _yelp_leads(origin: Tuple[float, float], radius_miles: float,
                cache) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in fetch_yelp_competitors(origin, radius_miles, cache):
        lat, lng = b.get("latitude"), b.get("longitude")
        dist = (round(haversine_miles(origin, (lat, lng)), 1)
                if lat is not None and lng is not None else None)
        out.append(_lead(
            b.get("name") or "", "yelp",
            phone=b.get("yelp_phone") or "", url=b.get("yelp_url") or "",
            distance_miles=dist, rating=b.get("yelp_rating"),
            review_count=b.get("yelp_review_count"),
            categories=b.get("yelp_categories") or []))
    return out


def _google_leads(origin: Optional[Tuple[float, float]], zip_code: str,
                  radius_miles: float, limit: int, cache
                  ) -> List[Dict[str, Any]]:
    if not GOOGLE_MAPS_API_KEY:
        return []
    try:
        from app.collectors.competitors import collect_competitors  # noqa: PLC0415
        from app.collectors.google_places import GooglePlacesClient  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        logger.warning(f"live_sourcing: google collector unavailable: {exc}")
        return []
    client = GooglePlacesClient(cache=cache)
    if origin is None:
        # No centroid — geolocate the ZIP off the first text result.
        origin = _geolocate_zip(client, zip_code)
    if origin is None:
        return []
    try:
        profiles = collect_competitors(
            client, origin, radius_miles, queries=list(GOOGLE_QUERIES),
            hydrate_top_n=min(limit, 8))
    except Exception as exc:
        logger.warning(f"live_sourcing: google search failed: {exc}")
        return []
    out: List[Dict[str, Any]] = []
    for p in profiles:
        # Google text search returns tangential hits ("Kismet") — keep only
        # names that actually read as CPR/first-aid businesses.
        if not _strong_name(p.name or ""):
            continue
        out.append(_lead(
            p.name or "", "google_places",
            phone=p.phone_number or "", url=p.website or "",
            address=p.formatted_address or "",
            distance_miles=p.distance_miles, rating=p.rating,
            review_count=p.user_ratings_total))
    return out


def _geolocate_zip(client, zip_code: str) -> Optional[Tuple[float, float]]:
    try:
        raw = client.text_search(str(zip_code), location=None, max_pages=1)
    except Exception:
        return None
    for r in raw or []:
        geo = (r.get("geometry") or {}).get("location") or {}
        lat, lng = geo.get("lat"), geo.get("lng")
        if lat is not None and lng is not None:
            return (float(lat), float(lng))
    return None


def _competitor_provider_leads(zip_code: str, limit: int = 8
                               ) -> List[Dict[str, Any]]:
    """Providers already teaching in this ZIP (from competitor_classes.csv).

    No API key needed — these are named organizations known to employ
    credentialed instructors in the target area.
    """
    ctx = competitor_context(zip_code)
    providers: List[Tuple[str, str]] = []
    seen = set()
    for course in ctx.get("courses", []) or []:
        locs = course.get("sample_locations") or []
        for i, prov in enumerate(course.get("providers") or []):
            key = prov.lower().strip()
            if key and key not in seen:
                seen.add(key)
                providers.append((prov, locs[0] if i < len(locs) and locs else ""))
    out: List[Dict[str, Any]] = []
    for prov, loc in providers[:limit]:
        lead = _lead(prov, "competitor_data", address=loc)
        lead["note"] = ("Provider already running CPR/BLS classes in this ZIP "
                        "— their instructors are proven local recruits.")
        out.append(lead)
    return out


def _dedupe(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge near-duplicate business names across sources (keep the richest)."""
    kept: List[Dict[str, Any]] = []
    for lead in leads:
        match = None
        for k in kept:
            if _name_jaccard(lead["name"], k["name"]) >= 0.6:
                match = k
                break
        if match is None:
            kept.append(lead)
            continue
        # Prefer the record with contact info / more fields; merge phone/url.
        if not match.get("phone") and lead.get("phone"):
            match["phone"] = lead["phone"]
        if not match.get("url") and lead.get("url"):
            match["url"] = lead["url"]
        if not match.get("address") and lead.get("address"):
            match["address"] = lead["address"]
        if lead["source"] not in match["source"]:
            match["source"] = f"{match['source']}+{lead['source']}"
        match["contactable"] = bool(match.get("phone") or match.get("url"))
    return kept


def _rank_key(lead: Dict[str, Any]):
    # Contactable + strong-signal + close + well-reviewed, roughly.
    dist = lead.get("distance_miles")
    dist = dist if dist is not None else 999.0
    return (
        1 if lead.get("contactable") else 0,
        1 if lead.get("strong_signal") else 0,
        -dist,
        lead.get("review_count") or 0,
    )


def find_live_instructor_leads(zip_code: str, radius_miles: float = 8.0,
                               limit: int = 12, cache: Any = None
                               ) -> Dict[str, Any]:
    """Real external CPR-business / provider leads near a ZIP.

    Combines Yelp + Google Places (both key-gated, cached) with the local
    competitor providers. Returns named, contactable, honest SIGNAL_ONLY leads.
    """
    zip5 = str(zip_code).strip().zfill(5)
    cache = cache if cache is not None else _make_cache()
    origin = load_zip_centroids().get(zip5)

    configured = {"yelp": yelp_configured(),
                  "google": bool(GOOGLE_MAPS_API_KEY)}
    sources_used: List[str] = []
    leads: List[Dict[str, Any]] = []

    if configured["yelp"] and origin:
        y = _yelp_leads(origin, radius_miles, cache)
        if y:
            sources_used.append("yelp")
        leads.extend(y)
    if configured["google"]:
        g = _google_leads(origin, zip5, radius_miles, limit, cache)
        if g:
            sources_used.append("google_places")
        leads.extend(g)
    provider_leads = _competitor_provider_leads(zip5)
    if provider_leads:
        sources_used.append("competitor_data")
    leads.extend(provider_leads)

    # Drop ALLCPR's own listings and anything far outside the search radius.
    max_dist = radius_miles * 2.5
    leads = [lead for lead in leads
             if lead["name"] and not _is_allcpr(lead["name"])
             and (lead.get("distance_miles") is None
                  or lead["distance_miles"] <= max_dist)]
    leads = _dedupe(leads)
    leads.sort(key=_rank_key, reverse=True)
    leads = leads[:limit]

    note = None
    if not configured["yelp"] and not configured["google"]:
        note = ("Live web sources (Yelp / Google Places) are not configured — "
                "set YELP_API_KEY / GOOGLE_MAPS_API_KEY to enable. Showing "
                "competitor-data providers only.")
    elif configured["yelp"] and not origin:
        note = ("No ZIP centroid on file, so Yelp (which needs coordinates) "
                "was skipped for this ZIP; Google Places + competitor data used.")

    return {
        "zip": zip5,
        "configured": configured,
        "sources_used": sources_used,
        "radius_miles": radius_miles,
        "count": len(leads),
        "leads": leads,
        "note": note,
    }
