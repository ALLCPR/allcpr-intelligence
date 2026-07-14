"""Public job-posting certification demand collector.

This module scans cited public job-posting rows from
``data/raw/job_postings.csv`` for CPR/BLS certification-demand signals near a
candidate location. When Adzuna is configured (``ADZUNA_APP_ID`` +
``ADZUNA_APP_KEY``), it also augments the CSV with live healthcare-role
postings from Adzuna's free API — only postings whose description mentions
BLS / CPR / AHA / First Aid / ACLS / PALS pass the filter.

The manual CSV always wins on dedup: a cited posting is more trustworthy
than an algorithmic search hit. If neither CSV nor Adzuna returns anything
for an area, all values stay unknown.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from typing import Dict, List, Optional

from app.collectors import adzuna_jobs
from app.config import RAW_DIR
from app.utils.cache import Cache
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger
from app.utils.report_safety import strip_sensitive_query_params
from app.utils.source_tracker import utcnow_iso

_CACHE: Optional[Cache] = None


def set_cache(cache: Optional[Cache]) -> None:
    """Configure the module-level cache. Pass None to disable."""
    global _CACHE
    _CACHE = cache

logger = get_logger(__name__)

JOB_POSTINGS_FILE = RAW_DIR / "job_postings.csv"

JOB_POSTING_COLUMNS = (
    "city",
    "state",
    "latitude",
    "longitude",
    "radius_miles",
    "employer",
    "title",
    "description",
    "source_url",
    "posted_at",
    "notes",
)

CERT_PATTERNS = {
    "bls": re.compile(r"\b(BLS|basic life support)\b", re.I),
    "cpr": re.compile(r"\bCPR\b", re.I),
    "first_aid": re.compile(r"\bfirst aid\b", re.I),
    "acls": re.compile(r"\bACLS\b", re.I),
    "pals": re.compile(r"\bPALS\b", re.I),
    "aha_red_cross": re.compile(
        r"\b(AHA|American Heart Association|Red Cross)\b", re.I,
    ),
}

ROLE_PATTERNS = {
    "healthcare_role": re.compile(
        r"\b(nurse|rn|lvn|lpn|medical assistant|clinician|patient care|"
        r"hospital|clinic|healthcare)\b",
        re.I,
    ),
    "emt_role": re.compile(r"\b(EMT|paramedic|ambulance)\b", re.I),
    "cna_role": re.compile(r"\b(CNA|certified nursing assistant)\b", re.I),
    "caregiver_role": re.compile(r"\b(caregiver|home health aide|care aide)\b", re.I),
    "dental_role": re.compile(r"\b(dental assistant|dental hygienist|dentist)\b", re.I),
    "childcare_role": re.compile(r"\b(childcare|daycare|preschool|teacher aide)\b", re.I),
}


def _parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _load_rows() -> List[Dict[str, str]]:
    if not JOB_POSTINGS_FILE.exists():
        return []
    try:
        with open(JOB_POSTINGS_FILE, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        logger.warning(f"job_postings: could not load {JOB_POSTINGS_FILE}: {exc}")
        return []
    if not rows:
        return []
    columns = set(rows[0].keys())
    missing = set(JOB_POSTING_COLUMNS) - columns
    if missing:
        logger.warning(
            f"job_postings: {JOB_POSTINGS_FILE} missing columns {sorted(missing)}"
        )
        return []
    return rows


def _empty_unknown() -> Dict[str, object]:
    return {
        "values": {
            "active_postings_count": None,
            "certification_postings_count": None,
            "bls_count": None,
            "cpr_count": None,
            "first_aid_count": None,
            "acls_count": None,
            "pals_count": None,
            "aha_red_cross_count": None,
            "healthcare_role_count": None,
            "emt_role_count": None,
            "cna_role_count": None,
            "caregiver_role_count": None,
            "dental_role_count": None,
            "childcare_role_count": None,
            "unique_employers_count": None,
        },
        "top_employers": [],
        "sample_postings": [],
        "sources": [{
            "name": "Public job postings CSV (not provided)",
            "url": "",
            "fields": [],
            "collected_at": utcnow_iso(),
            "notes": "unknown — add cited public postings to data/raw/job_postings.csv",
        }],
    }


def _matches_candidate(row: Dict[str, str], city: str, state: str,
                       latitude: float, longitude: float) -> Optional[float]:
    row_city = (row.get("city") or "").strip().lower()
    row_state = (row.get("state") or "").strip().upper()
    if row_city and row_city != city.lower():
        return None
    if row_state and row_state != state.upper():
        return None

    row_lat = _parse_float(row.get("latitude"))
    row_lon = _parse_float(row.get("longitude"))
    radius = _parse_float(row.get("radius_miles"))
    if row_lat is None or row_lon is None or radius is None:
        return None
    distance = haversine_miles((latitude, longitude), (row_lat, row_lon))
    if distance > radius:
        return None
    return distance


def _merge_csv_and_adzuna(
    csv_rows: List[Dict[str, str]],
    adzuna_rows: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Merge with CSV precedence — cited postings are more trustworthy than
    algorithmic search hits, so they always win on dedup-by-source-url."""
    if not adzuna_rows:
        return csv_rows
    seen_urls: set = {
        (row.get("source_url") or "").strip().lower()
        for row in csv_rows
        if (row.get("source_url") or "").strip()
    }
    merged = list(csv_rows)
    for row in adzuna_rows:
        url = (row.get("source_url") or "").strip().lower()
        if url and url in seen_urls:
            continue
        merged.append(row)
        if url:
            seen_urls.add(url)
    return merged


def collect_job_posting_demand(
    city: str,
    state: str,
    latitude: float,
    longitude: float,
) -> Dict[str, object]:
    """Collect job-posting certification demand from cited CSV + Adzuna live API."""
    csv_rows = _load_rows()
    adzuna_rows: List[Dict[str, str]] = []
    if adzuna_jobs.is_configured():
        try:
            adzuna_rows = adzuna_jobs.fetch_adzuna_postings(
                city=city, state=state,
                latitude=latitude, longitude=longitude,
                cache=_CACHE,
            )
        except Exception as exc:
            logger.warning(f"job_postings: Adzuna fetch failed: {exc}")
            adzuna_rows = []

    rows = _merge_csv_and_adzuna(csv_rows, adzuna_rows)
    if not rows:
        return _empty_unknown()

    matched: List[Dict[str, object]] = []
    for row in rows:
        distance = _matches_candidate(row, city, state, latitude, longitude)
        if distance is None:
            continue
        text = " ".join([
            row.get("employer") or "",
            row.get("title") or "",
            row.get("description") or "",
            row.get("notes") or "",
        ])
        cert_hits = {
            name: bool(pattern.search(text))
            for name, pattern in CERT_PATTERNS.items()
        }
        role_hits = {
            name: bool(pattern.search(text))
            for name, pattern in ROLE_PATTERNS.items()
        }
        matched.append({
            "row": row,
            "distance_miles": round(distance, 3),
            "cert_hits": cert_hits,
            "role_hits": role_hits,
            "has_certification_signal": any(cert_hits.values()),
        })

    values: Dict[str, object] = {
        "active_postings_count": len(matched),
        "certification_postings_count": sum(
            1 for m in matched if m["has_certification_signal"]
        ),
        "unique_employers_count": len({
            (m["row"].get("employer") or "").strip().lower()
            for m in matched if (m["row"].get("employer") or "").strip()
        }),
    }
    for key in CERT_PATTERNS:
        values[f"{key}_count"] = sum(1 for m in matched if m["cert_hits"].get(key))
    for key in ROLE_PATTERNS:
        values[f"{key}_count"] = sum(1 for m in matched if m["role_hits"].get(key))

    employers = Counter(
        (m["row"].get("employer") or "unknown").strip() or "unknown"
        for m in matched
    )
    sample = []
    for m in matched[:10]:
        row = m["row"]
        sample.append({
            "employer": row.get("employer") or "unknown",
            "title": row.get("title") or "unknown",
            "source_url": strip_sensitive_query_params(row.get("source_url") or ""),
            "posted_at": row.get("posted_at") or "unknown",
            "distance_miles": m["distance_miles"],
            "certification_signals": [
                name for name, hit in m["cert_hits"].items() if hit
            ],
            "role_signals": [
                name for name, hit in m["role_hits"].items() if hit
            ],
        })

    fields = [
        f"job_demand.{key}" for key, value in values.items()
        if value is not None
    ]
    source_urls = [
        strip_sensitive_query_params(m["row"].get("source_url") or "")
        for m in matched if m["row"].get("source_url")
    ]
    sources: List[Dict[str, object]] = []
    if csv_rows:
        sources.append({
            "name": "Public job postings CSV (data/raw/job_postings.csv)",
            "url": str(JOB_POSTINGS_FILE),
            "fields": fields,
            "collected_at": utcnow_iso(),
            "notes": (
                "user-supplied public job posting rows scanned for CPR/BLS/"
                "First Aid/AHA/Red Cross certification keywords"
            ),
        })
    if adzuna_rows:
        sources.append({
            "name": "Adzuna jobs API (live)",
            "url": " | ".join(source_urls[:5]) or "https://api.adzuna.com",
            "fields": fields,
            "collected_at": utcnow_iso(),
            "notes": (
                f"{len(adzuna_rows)} live healthcare-role posting(s) within "
                f"~5mi, filtered to those mentioning BLS/CPR/AHA/First Aid"
            ),
        })
    if not sources:
        sources.append({
            "name": "Public job postings CSV (data/raw/job_postings.csv)",
            "url": " | ".join(source_urls[:5]) or str(JOB_POSTINGS_FILE),
            "fields": fields,
            "collected_at": utcnow_iso(),
            "notes": (
                "user-supplied public job posting rows scanned for CPR/BLS/"
                "First Aid/AHA/Red Cross certification keywords"
            ),
        })
    return {
        "values": values,
        "top_employers": [
            {"employer": name, "posting_count": count}
            for name, count in employers.most_common(10)
        ],
        "sample_postings": sample,
        "sources": sources,
    }
