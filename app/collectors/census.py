"""
US Census ACS 5-year collector.

Pulls population, median household income, age structure, healthcare-related
employment, and educational attainment for the geography (place or county)
containing the candidate point.

Resilience:
  - If the full variable bundle fails (often: a single ID isn't published for
    that geography), fall back to a minimal "core" bundle that almost always
    returns. We never fabricate values; failed fields stay None.
  - Distinguishes empty body / non-JSON from HTTP errors and logs a preview.

Docs: https://www.census.gov/data/developers/data-sets/acs-5year.html
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import requests

from app.config import (
    CENSUS_API_KEY,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF_SECONDS,
)
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso
from typing import Optional as _Optional
from app.config import ttl_for as _ttl_for
from app.utils.cache import Cache as _Cache, cached_call as _cached_call

logger = get_logger(__name__)

_CACHE: _Optional[_Cache] = None


def set_cache(cache: _Optional[_Cache]) -> None:
    """Configure the module-level cache. Pass None to disable."""
    global _CACHE
    _CACHE = cache


ACS_YEAR = 2022
ACS_BASE = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
GEOCODER_BASE = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"


# ACS variable IDs we want. The "core" set is known to be reliable for any
# state/place/county. The "ext" set adds richer indicators that may not exist
# in every geography; we degrade gracefully when they fail.
CORE_VARS: Dict[str, str] = {
    "population":                "B01003_001E",
    "median_household_income":   "B19013_001E",
    "median_age":                "B01002_001E",
    "working_age_pop_16plus":    "B23025_001E",
    "employed_pop":              "B23025_004E",
    "edu_total_25plus":          "B15003_001E",
}

# Bachelor's-or-higher = sum of B15003_022..025 (bachelor's, master's,
# professional, doctorate). The summed approach is more robust than picking
# a subject table.
BACHELORS_PLUS_VARS = ("B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E")

# Healthcare & social-assistance employment (industry table). Only populated
# at some geographies; treated as best-effort.
EXT_VARS: Dict[str, str] = {
    "employed_health_social":    "C24050_046E",   # Hlth+SocAsst (Total)
}

ALL_VARS = {**CORE_VARS, **EXT_VARS}


class CensusError(RuntimeError):
    pass


def _request_json(url: str, params: Optional[Dict] = None) -> Optional[list]:
    """GET with retry. Returns parsed JSON list, or None on any failure."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(f"Census transient error "
                           f"(attempt {attempt}/{MAX_RETRIES}): {exc}. "
                           f"Retrying in {wait:.1f}s.")
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(f"Census HTTP {resp.status_code} on attempt {attempt}; "
                           f"retrying in {wait:.1f}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            preview = (resp.text or "")[:200].replace("\n", " ")
            logger.warning(f"Census HTTP {resp.status_code}: {preview!r}")
            return None

        body = (resp.text or "").strip()
        if not body:
            logger.warning("Census returned empty body (likely an invalid variable "
                           "or geography for this request).")
            return None
        try:
            return resp.json()
        except ValueError as exc:
            preview = body[:200].replace("\n", " ")
            logger.warning(f"Census non-JSON body: {exc}. Body preview: {preview!r}")
            return None
    logger.warning(f"Census request failed after {MAX_RETRIES} attempts: {last_exc}")
    return None


def _coords_to_tract(latitude: float, longitude: float
                     ) -> Optional[Tuple[str, str, str]]:
    """Resolve (lat, lon) to (state_fips, county_fips, tract_fips).

    Tract-level granularity is what lets economy_score actually differ
    between candidates inside one city. Without it, every SF candidate
    resolves to the same place=San Francisco geography and gets the same
    median income / age / healthcare-employment share.
    """
    params = {
        "x": longitude,
        "y": latitude,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Census Tracts",
        "format": "json",
    }
    try:
        resp = requests.get(GEOCODER_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Census geocoder (tract) failed: {exc}")
        return None
    tracts = (data.get("result", {})
                  .get("geographies", {})
                  .get("Census Tracts", []))
    if not tracts:
        return None
    t = tracts[0]
    state = t.get("STATE")
    county = t.get("COUNTY")
    tract = t.get("TRACT")
    if state and county and tract:
        return state, county, tract
    return None


def _coords_to_place(latitude: float, longitude: float
                     ) -> Optional[Tuple[str, str]]:
    """Resolve (lat, lon) to (state_fips, place_fips). None if no place."""
    params = {
        "x": longitude,
        "y": latitude,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Incorporated Places",
        "format": "json",
    }
    try:
        resp = requests.get(GEOCODER_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Census geocoder (place) failed: {exc}")
        return None
    places = (data.get("result", {})
                  .get("geographies", {})
                  .get("Incorporated Places", []))
    if not places:
        return None
    p = places[0]
    state_fips = p.get("STATE")
    place_fips = p.get("PLACE")
    if state_fips and place_fips:
        return state_fips, place_fips
    return None


def _coords_to_county(latitude: float, longitude: float
                      ) -> Optional[Tuple[str, str]]:
    params = {
        "x": longitude,
        "y": latitude,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Counties",
        "format": "json",
    }
    try:
        resp = requests.get(GEOCODER_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Census geocoder (county) failed: {exc}")
        return None
    counties = (data.get("result", {})
                    .get("geographies", {})
                    .get("Counties", []))
    if not counties:
        return None
    c = counties[0]
    if c.get("STATE") and c.get("COUNTY"):
        return c["STATE"], c["COUNTY"]
    return None


def _fetch_acs(var_ids: List[str], geo_clause: Dict[str, str]) -> Optional[List]:
    params = {
        "get": ",".join(var_ids),
        **geo_clause,
    }
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    return _request_json(ACS_BASE, params=params)


def _row_to_dict(header: List[str], row: List[str]) -> Dict[str, Optional[float]]:
    """Census responses are positional; pair header IDs with row values."""
    out: Dict[str, Optional[float]] = {}
    for idx, var_id in enumerate(header):
        if idx >= len(row):
            continue
        raw = row[idx]
        if raw in (None, "", "null", "-666666666", "-999999999"):
            out[var_id] = None
            continue
        try:
            out[var_id] = float(raw)
        except (TypeError, ValueError):
            out[var_id] = None
    return out


def fetch_demographics(latitude: float, longitude: float
                       ) -> Tuple[Dict[str, Optional[float]], List[str], str]:
    """Cached wrapper for the live Census fetch. Same return shape as before."""
    # geo_resolution bumped to "tract_v1" — invalidates pre-tract-level cache
    # entries that resolved every coord to the same incorporated place.
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "geo_resolution": "tract_v1",
    }
    value, _ = _cached_call(
        _CACHE, "census", "fetch_demographics", params,
        ttl_seconds=_ttl_for("census", "fetch_demographics"),
        live_call=lambda: _fetch_demographics_live(latitude, longitude),
    )
    return value


def _fetch_demographics_live(latitude: float, longitude: float
                             ) -> Tuple[Dict[str, Optional[float]], List[str], str]:
    """
    Fetch a small bundle of ACS demographics for the geography containing
    (lat, lon). Returns (values, citation_urls, geo_desc).

    Geography precedence: incorporated place -> county.
    """
    keys = list(ALL_VARS.keys())
    values: Dict[str, Optional[float]] = {k: None for k in keys}
    values["edu_bachelors_or_higher_25plus"] = None
    citations: List[str] = []

    geo: Optional[Dict[str, str]] = None
    geo_desc = "unresolved"

    # Tract first — gives intra-city differentiation. Place / county are
    # fallbacks for thin tracts or rural areas.
    tract = _coords_to_tract(latitude, longitude)
    if tract:
        state_fips, county_fips, tract_fips = tract
        geo = {
            "for": f"tract:{tract_fips}",
            "in": f"state:{state_fips} county:{county_fips}",
        }
        geo_desc = f"tract {state_fips}{county_fips}{tract_fips}"
    else:
        place = _coords_to_place(latitude, longitude)
        if place:
            state_fips, place_fips = place
            geo = {"for": f"place:{place_fips}", "in": f"state:{state_fips}"}
            geo_desc = f"place {state_fips}{place_fips}"
        else:
            county = _coords_to_county(latitude, longitude)
            if county:
                state_fips, county_fips = county
                geo = {"for": f"county:{county_fips}", "in": f"state:{state_fips}"}
                geo_desc = f"county {state_fips}{county_fips}"

    if not geo:
        logger.warning("Census: could not resolve coordinates to any geography.")
        return values, citations, geo_desc

    # ---- Pass 1: full bundle ------------------------------------------------
    full_var_ids = list(ALL_VARS.values()) + list(BACHELORS_PLUS_VARS)
    data = _fetch_acs(full_var_ids, geo)

    # ---- Pass 2: core only (drop ext variables that may not exist) ----------
    if not data or len(data) < 2:
        logger.warning(f"Census: full bundle failed for {geo_desc}; "
                       f"falling back to core variables.")
        core_ids = list(CORE_VARS.values()) + list(BACHELORS_PLUS_VARS)
        data = _fetch_acs(core_ids, geo)

    # ---- Pass 3: minimal (just pop / income / age) --------------------------
    if not data or len(data) < 2:
        logger.warning(f"Census: core bundle failed for {geo_desc}; "
                       f"trying minimal pop/income/age only.")
        minimal_ids = [
            CORE_VARS["population"],
            CORE_VARS["median_household_income"],
            CORE_VARS["median_age"],
        ]
        data = _fetch_acs(minimal_ids, geo)

    if not data or len(data) < 2:
        logger.warning(f"Census: all fetches failed for {geo_desc}.")
        return values, citations, geo_desc

    header_row, row = data[0], data[1]
    raw_by_id = _row_to_dict(header_row, row)

    for key, var_id in ALL_VARS.items():
        if var_id in raw_by_id:
            values[key] = raw_by_id[var_id]

    bach_sum = 0.0
    bach_seen = False
    for v in BACHELORS_PLUS_VARS:
        x = raw_by_id.get(v)
        if isinstance(x, (int, float)):
            bach_sum += x
            bach_seen = True
    values["edu_bachelors_or_higher_25plus"] = bach_sum if bach_seen else None

    citations.append(
        f"{ACS_BASE}?get={','.join(full_var_ids)}&" +
        "&".join(f"{k}={v}" for k, v in geo.items())
    )
    return values, citations, geo_desc


def derive_indicators(values: Dict[str, Optional[float]]
                      ) -> Dict[str, Optional[float]]:
    """Convert raw ACS values into derived ratios. Missing inputs => None."""
    pop = values.get("population")
    emp = values.get("employed_pop")
    emp_health = values.get("employed_health_social")
    edu_bach = values.get("edu_bachelors_or_higher_25plus")
    edu_total = values.get("edu_total_25plus")
    work_age = values.get("working_age_pop_16plus")

    return {
        "healthcare_employment_share": (
            emp_health / emp if emp and emp_health is not None and emp > 0 else None
        ),
        "bachelors_or_higher_share": (
            edu_bach / edu_total
            if edu_total and edu_bach is not None and edu_total > 0 else None
        ),
        "working_age_share": (
            work_age / pop if pop and work_age is not None and pop > 0 else None
        ),
        "employment_rate": (
            emp / work_age if work_age and emp is not None and work_age > 0 else None
        ),
    }


def collect_economy(latitude: float, longitude: float
                    ) -> Dict[str, object]:
    """Top-level entry point used by the economy enricher."""
    values, citations, geo_desc = fetch_demographics(latitude, longitude)
    indicators = derive_indicators(values)

    populated_fields = [k for k, v in values.items() if v is not None]
    sources = []
    if citations:
        sources.append({
            "name": f"US Census Bureau ACS 5-year ({ACS_YEAR})",
            "url": citations[0],
            "fields": populated_fields,
            "collected_at": utcnow_iso(),
            "notes": f"geography: {geo_desc}",
        })

    return {
        "values": values,
        "indicators": indicators,
        "sources": sources,
        "geo_desc": geo_desc,
    }
