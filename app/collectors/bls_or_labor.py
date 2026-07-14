"""
BLS / labor market collector — BLS QCEW (Quarterly Census of Employment and
Wages) county-level health-care employment.

The QCEW data API is free, requires no key, and returns one JSON file per
county per quarter listing every NAICS industry. For each candidate we:

  1. Resolve the candidate's (lat, lon) to a state+county FIPS via the Census
     geocoder (re-using the helper already in ``census.py``).
  2. Try the most recently-released quarters in order until one returns data.
  3. Pull the single record with ``industry_code == "62"`` (Health Care &
     Social Assistance) and ``own_code == "5"`` (private). That row carries
     the employment count, average weekly wage and a pre-computed location
     quotient versus the national average.

Failures (county unresolved, all quarters empty, network error) preserve the
historical "stub" source name so ``confidence_score._trust`` returns 0 for
them — we never assert trust for data we did not actually fetch.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.collectors.census import _coords_to_county
from app.config import MAX_RETRIES, REQUEST_TIMEOUT, RETRY_BACKOFF_SECONDS, ttl_for as _ttl_for
from app.utils.cache import Cache as _Cache, build_cache_key as _build_cache_key
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)

_CACHE: Optional[_Cache] = None


def set_cache(cache: Optional[_Cache]) -> None:
    global _CACHE
    _CACHE = cache


STUB_FIELDS = (
    "healthcare_employment_count",
    "healthcare_employment_lq",
    "avg_weekly_wage_healthcare",
    "data_year",
)

# NAICS 62 = Health Care and Social Assistance.
_HEALTHCARE_NAICS = "62"
# QCEW ownership codes: "5" = Private. (Other ownership codes split out
# Federal / State / Local government employment — we want the private slice
# because that's where CPR training demand comes from.)
_PRIVATE_OWN_CODE = "5"

# How many recent quarters to try before giving up. QCEW lags ~6-9 months.
_MAX_QUARTERS_TO_TRY = 6


def _candidate_quarters(today: Optional[date] = None) -> List[Tuple[int, int]]:
    """Return (year, quarter) pairs to try, most recent first.

    QCEW typically publishes a quarter ~6 months after it ends; we start two
    quarters back from "now" to avoid hammering the API for data that does
    not exist yet, then walk further back.
    """
    today = today or date.today()
    current_q = (today.month - 1) // 3 + 1
    year, quarter = today.year, current_q
    # Step back two quarters as the most recent likely-published candidate.
    for _ in range(2):
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    out: List[Tuple[int, int]] = []
    for _ in range(_MAX_QUARTERS_TO_TRY):
        out.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return out


def _qcew_url(year: int, quarter: int, area_code: str) -> str:
    # The .json variant of this endpoint was retired (returns 404). The .csv
    # "slim" variant still serves one row per industry/ownership for the
    # area-quarter. We parse CSV and derive avg weekly wage ourselves.
    return (
        f"https://data.bls.gov/cew/data/api/{year}/{quarter}/area/"
        f"{area_code}.csv"
    )


def _fetch_qcew(url: str) -> Optional[List[Dict[str, Any]]]:
    """Cached wrapper for the live BLS QCEW fetch.

    Never caches None: a None return means "not published yet" (HTTP 404/204).
    Caching that for the BLS TTL would block newly-released quarters for up
    to a year. Only successful fetches go in the cache.
    """
    if _CACHE is None:
        return _fetch_qcew_live(url)
    key = _build_cache_key("bls_qcew", "fetch_qcew", {"url": url})
    hit = _CACHE.get(key)
    if hit is not None:
        return hit.value
    value = _fetch_qcew_live(url)
    if value is not None:
        _CACHE.set(key, value, ttl_seconds=_ttl_for("bls_qcew", "fetch_qcew"),
                   provider="bls_qcew")
    return value


def _fetch_qcew_live(url: str) -> Optional[List[Dict[str, Any]]]:
    """GET the QCEW area file and return its ``data`` list, or None on error."""
    headers = {"User-Agent": "allcpr-site-intel/1.0 (BLS QCEW collector)"}
    delay = 0.0
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if response.status_code == 200:
                body = response.text or ""
                if not body.strip():
                    return None
                try:
                    reader = csv.DictReader(io.StringIO(body))
                    rows = [dict(r) for r in reader]
                except Exception:
                    return None
                return rows or None
            if response.status_code in (404, 204):
                return None  # quarter not released yet
            if response.status_code >= 500:
                last_exc = RuntimeError(f"BLS QCEW {response.status_code}")
            else:
                return None
        delay = max(delay, RETRY_BACKOFF_SECONDS) * (1.5 if attempt else 1)
        if attempt < MAX_RETRIES - 1:
            import time
            time.sleep(delay)
    if last_exc is not None:
        logger.warning(f"bls_or_labor: QCEW fetch failed for {url}: {last_exc}")
    return None


def _healthcare_record(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the NAICS 62 private record from a QCEW area response."""
    for row in rows:
        if (str(row.get("industry_code")) == _HEALTHCARE_NAICS
                and str(row.get("own_code")) == _PRIVATE_OWN_CODE):
            return row
    return None


def _coerce_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _stub_block(notes: str) -> Dict[str, object]:
    """Same shape as the original stub; keeps confidence trust at 0.0."""
    return {
        "values": {k: None for k in STUB_FIELDS},
        "indicators": {},
        "sources": [{
            "name": "BLS / labor market (not yet integrated)",
            "url": "",
            "fields": [],
            "collected_at": utcnow_iso(),
            "notes": f"stub — {notes}",
        }],
    }


def collect_labor(latitude: float, longitude: float,
                  state_fips: Optional[str] = None,
                  county_fips: Optional[str] = None,
                  ) -> Dict[str, object]:
    """Collect county-level healthcare employment from BLS QCEW.

    Returns the canonical shape expected by the economy enricher. On failure
    (county unresolved, all candidate quarters empty, network error), returns
    the original "stub" shape so the rest of the system keeps treating the
    fields as ``unknown`` rather than fabricating a number.
    """
    if state_fips is None or county_fips is None:
        county = _coords_to_county(latitude, longitude)
        if county is not None:
            state_fips, county_fips = county
    if state_fips is None or county_fips is None:
        return _stub_block(
            "could not resolve county FIPS for "
            f"({latitude:.4f},{longitude:.4f})"
        )

    area_code = f"{state_fips}{county_fips}"
    tried: List[str] = []
    for year, quarter in _candidate_quarters():
        url = _qcew_url(year, quarter, area_code)
        rows = _fetch_qcew(url)
        if not rows:
            tried.append(f"{year}Q{quarter}=miss")
            continue
        healthcare = _healthcare_record(rows)
        if healthcare is None:
            tried.append(f"{year}Q{quarter}=no-NAICS62")
            continue
        # Found it. Employment = 3rd-month level; fall back to a 3-month avg.
        emp = _coerce_int(healthcare.get("month3_emplvl"))
        m1 = _coerce_int(healthcare.get("month1_emplvl"))
        m2 = _coerce_int(healthcare.get("month2_emplvl"))
        avg_emp = None
        emp_levels = [v for v in (m1, m2, emp) if v]
        if emp_levels:
            avg_emp = sum(emp_levels) / len(emp_levels)
        # The slim CSV carries no avg_wkly_wage or location quotient. Derive
        # avg weekly wage from total quarterly wages / avg employment / 13
        # weeks. LQ needs national totals we don't fetch → stays None.
        wage = _coerce_float(healthcare.get("avg_wkly_wage"))
        if wage is None:
            total_wages = _coerce_float(healthcare.get("total_qtrly_wages"))
            if total_wages and avg_emp and avg_emp > 0:
                wage = round(total_wages / avg_emp / 13.0, 2)
        lq = _coerce_float(healthcare.get("lq_month3_emplvl"))
        values: Dict[str, object] = {
            "healthcare_employment_count": emp or (_coerce_int(avg_emp)),
            "healthcare_employment_lq": lq,
            "avg_weekly_wage_healthcare": wage,
            "data_year": int(year),
        }
        populated = [k for k, v in values.items() if v is not None]
        return {
            "values": values,
            "indicators": {
                "qcew_area_code": area_code,
                "qcew_quarter": f"{year}Q{quarter}",
                "qcew_own_code": _PRIVATE_OWN_CODE,
                "qcew_industry_code": _HEALTHCARE_NAICS,
            },
            "sources": [{
                "name": "BLS QCEW (Quarterly Census of Employment and Wages)",
                "url": url,
                "fields": populated,
                "collected_at": utcnow_iso(),
                "notes": (
                    f"County area {area_code}, NAICS 62 (health care), "
                    f"private ownership, {year} Q{quarter}"
                ),
            }],
        }

    return _stub_block(
        f"BLS QCEW returned no usable rows for area {area_code} "
        f"across {len(tried)} quarters tried"
    )
