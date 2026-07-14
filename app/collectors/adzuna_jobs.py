"""
Adzuna jobs API — automates B2B certification-demand evidence.

The manual ``data/raw/job_postings.csv`` flow lets ALLCPR drop in cited
public job postings as proof of employer-side CPR/BLS demand. That works
but is slow — someone has to hand-curate postings per metro. Adzuna's
free-tier API (250 calls / month) lets us pull live healthcare-role
postings near each candidate automatically.

Design:
- One module-level entry point ``fetch_adzuna_postings(city, state,
  latitude, longitude, radius_miles)`` returns rows in the same dict shape
  as ``job_postings.JOB_POSTING_COLUMNS`` — so the existing scanner in
  ``job_postings.collect_job_posting_demand`` consumes them unchanged.
- Feature-flagged on ``ADZUNA_APP_ID`` + ``ADZUNA_APP_KEY``. When either
  is missing, the function returns ``[]`` and the manual-CSV path is the
  only source.
- Cached aggressively (30-day TTL) to stay well under the 250-call quota.
- Filters server-side by location + ``where=lat,lng,radius_miles``; then
  client-side by description matching CPR/BLS/AHA/First Aid keywords so
  we only count *certification-relevant* roles.

Adzuna docs: https://developer.adzuna.com/docs/search
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


ADZUNA_APP_ID: str = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY: str = os.getenv("ADZUNA_APP_KEY", "")
ADZUNA_COUNTRY: str = os.getenv("ADZUNA_COUNTRY", "us").lower()
ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"
ADZUNA_TTL_SECONDS = 30 * 86400

# Healthcare role keywords we search Adzuna for. Each hits the
# `what_or` parameter (any-of match) so a single API call covers several
# roles at once. We split into a couple of buckets to keep the per-call
# result set manageable.
ROLE_QUERY_BUCKETS: Tuple[Tuple[str, str], ...] = (
    ("clinical",
     "registered nurse LVN LPN medical assistant patient care"),
    ("emt_paramedic",
     "EMT paramedic ambulance"),
    ("nursing_aide",
     "CNA nursing assistant caregiver home health aide"),
    ("dental",
     "dental assistant dental hygienist"),
    ("childcare",
     "childcare daycare preschool teacher aide"),
)

# Certification keywords. A posting is only counted as certification-relevant
# if its title/description matches one of these (mirrored from
# job_postings.CERT_PATTERNS so the downstream scanner sees the same shape).
_CERT_KEEP_RE = re.compile(
    r"\b(BLS|basic life support|CPR|first aid|AHA|"
    r"american heart association|red cross|ACLS|PALS)\b",
    re.I,
)


def is_configured() -> bool:
    return bool(ADZUNA_APP_ID and ADZUNA_APP_KEY)


def _adzuna_request(
    bucket_label: str,
    what_or: str,
    where: str,
    radius_miles: float,
    results_per_page: int = 50,
) -> List[Dict[str, object]]:
    """One Adzuna search call. Returns the ``results`` list or ``[]``.

    ``where`` must be a place name like ``"San Francisco"`` — Adzuna's
    ``where`` parameter returns HTTP 503 for raw ``lat,lng`` strings.
    Per-result lat/lon is returned in each job posting and is used by
    the caller for radius filtering.
    """
    if not is_configured():
        return []
    url = f"{ADZUNA_BASE}/{ADZUNA_COUNTRY}/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": int(results_per_page),
        "what_or": what_or,
        "where": where,
        "distance": float(radius_miles),
        "content-type": "application/json",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning(f"adzuna({bucket_label}): request failed: {exc}")
        return []
    if resp.status_code >= 400:
        # Adzuna returns the app_id + app_key only in the request, so
        # redact them from any error preview before logging.
        preview = (resp.text or "")[:200]
        if ADZUNA_APP_KEY and ADZUNA_APP_KEY in preview:
            preview = preview.replace(ADZUNA_APP_KEY, "<key>")
        if ADZUNA_APP_ID and ADZUNA_APP_ID in preview:
            preview = preview.replace(ADZUNA_APP_ID, "<id>")
        logger.warning(
            f"adzuna({bucket_label}): HTTP {resp.status_code}: {preview!r}"
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning(f"adzuna({bucket_label}): non-JSON body")
        return []
    return data.get("results") or []


def _normalize_posting(
    raw: Dict[str, object],
    city: str,
    state: str,
) -> Optional[Dict[str, str]]:
    """Adzuna result → row matching ``job_postings.JOB_POSTING_COLUMNS``."""
    title = str(raw.get("title") or "")
    description = str(raw.get("description") or "")
    employer = str((raw.get("company") or {}).get("display_name") or "")
    text = " ".join([title, description, employer])
    if not _CERT_KEEP_RE.search(text):
        return None  # Not certification-relevant — skip.

    loc = raw.get("location") or {}
    latitude = raw.get("latitude")
    longitude = raw.get("longitude")
    if not (isinstance(latitude, (int, float))
            and isinstance(longitude, (int, float))):
        return None
    source_url = str(raw.get("redirect_url") or raw.get("adref") or "")
    posted_at = str(raw.get("created") or "")

    return {
        "city": city,
        "state": state,
        "latitude": str(latitude),
        "longitude": str(longitude),
        # 3mi credit radius: an SF Mission hospital's CPR-required nurse
        # posting is real B2B partnership evidence for any ALLCPR candidate
        # within ~10-min drive. Manual cited postings in the CSV can
        # narrow this with their own per-row radius.
        "radius_miles": "3",
        "employer": employer or "unknown",
        "title": title or "unknown",
        "description": description or "",
        "source_url": source_url,
        "posted_at": posted_at,
        "notes": f"adzuna:{(loc.get('display_name') or 'unknown')}",
    }


def fetch_adzuna_postings(
    city: str,
    state: str,
    latitude: float,
    longitude: float,
    radius_miles: float = 5.0,
    cache: Optional[Cache] = None,
) -> List[Dict[str, str]]:
    """Fetch CPR/BLS-relevant healthcare postings near a candidate.

    Returns rows in the same dict shape as ``job_postings.JOB_POSTING_COLUMNS``.
    Empty when Adzuna isn't configured or returns nothing.

    Adzuna requires a place-name ``where`` parameter (raw ``lat,lng``
    returns HTTP 503). We query by ``"city, ST"`` and rely on Adzuna's
    per-result lat/lng for the actual radius filtering downstream in
    ``job_postings.collect_job_posting_demand`` (which calls
    ``_matches_candidate`` against each row's ``latitude``/``longitude``).
    """
    if not is_configured():
        return []
    where = f"{city}, {state}" if state else city

    cache_params = {
        "country": ADZUNA_COUNTRY,
        "where": where,
        "radius_miles": radius_miles,
        "buckets": [b[0] for b in ROLE_QUERY_BUCKETS],
    }

    def _live() -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        seen_urls: set = set()
        for bucket_label, what_or in ROLE_QUERY_BUCKETS:
            results = _adzuna_request(
                bucket_label=bucket_label,
                what_or=what_or,
                where=where,
                radius_miles=radius_miles,
            )
            for raw in results:
                row = _normalize_posting(raw, city=city, state=state)
                if row is None:
                    continue
                url = row.get("source_url") or ""
                key = url or f"{row['title']}|{row['employer']}|{row['latitude']}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                rows.append(row)
        return rows

    value, _ = cached_call(
        cache, "adzuna", "search_certified_postings",
        cache_params, ttl_seconds=ADZUNA_TTL_SECONDS,
        live_call=_live,
    )
    logger.info(
        f"adzuna: returning {len(value or [])} certification-relevant "
        f"posting(s) near {where}"
    )
    return list(value or [])
