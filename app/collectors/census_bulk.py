"""
Bulk Census ACS collector — one national pull for ALL ~33k ZCTAs.

The per-point :mod:`app.collectors.census` collector resolves one candidate's
coordinates to a tract/place/county. For the national modeled-demand layer we
instead want every ZIP Code Tabulation Area at once, which the ACS 5-year API
supports directly via ``for=zip code tabulation area:*``. That is a handful of
HTTP calls (not 33k), free, and cached for a year so re-runs are instant.

We never fabricate values: Census "jam values" (large negative sentinels for
suppressed/missing data) and division-by-zero cases resolve to ``None`` so the
downstream score drops that signal and renormalizes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.collectors.census import _request_json
from app.config import CENSUS_API_KEY, CACHE_TTL_DEFAULT_SECONDS
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# ACS 5-year vintage. 2024 is the newest stable ACS 5-year release with ZCTA
# geography available through the Census API.
DEFAULT_ACS_YEAR = 2024

# Raw ACS variable IDs we pull, then reduce to shares.
_VARS = {
    "population":              "B01003_001E",
    "median_household_income": "B19013_001E",
    "pop_16plus":              "B23025_001E",
    "employed":                "B23025_004E",
    "edu_total_25plus":        "B15003_001E",
    "employed_health_social":  "C24050_046E",
}
_BACHELORS_PLUS = ("B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E")

# Census suppresses small cells with large negative sentinels; treat as missing.
_JAM_FLOOR = -1e6


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out <= _JAM_FLOOR:   # Census jam value / suppressed
        return None
    return out


def _safe_share(numerator: Optional[float], denom: Optional[float]
                ) -> Optional[float]:
    if numerator is None or denom is None or denom <= 0:
        return None
    return numerator / denom


def _acs_base(year: int) -> str:
    return f"https://api.census.gov/data/{year}/acs/acs5"


def _fetch_live(year: int, api_key: str) -> Dict[str, Dict[str, Optional[float]]]:
    """Pull every ZCTA once and reduce raw counts to the shares we score on."""
    get_vars: List[str] = list(_VARS.values()) + list(_BACHELORS_PLUS)
    params = {
        "get": ",".join(get_vars),
        "for": "zip code tabulation area:*",
    }
    if api_key:
        params["key"] = api_key
    rows = _request_json(_acs_base(year), params=params)
    if not rows or len(rows) < 2:
        logger.warning("Bulk ACS ZCTA pull returned no usable rows.")
        return {}

    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    zcta_col = idx.get("zip code tabulation area")
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for row in rows[1:]:
        try:
            zip_code = str(row[zcta_col]).zfill(5)
        except (IndexError, TypeError):
            continue
        if len(zip_code) != 5 or not zip_code.isdigit():
            continue

        def col(var: str) -> Optional[float]:
            i = idx.get(var)
            return _num(row[i]) if i is not None and i < len(row) else None

        population = col(_VARS["population"])
        pop_16plus = col(_VARS["pop_16plus"])
        employed = col(_VARS["employed"])
        edu_total = col(_VARS["edu_total_25plus"])
        health = col(_VARS["employed_health_social"])
        bachelors = sum(
            (v for v in (col(b) for b in _BACHELORS_PLUS) if v is not None),
            0.0,
        ) if any(col(b) is not None for b in _BACHELORS_PLUS) else None

        out[zip_code] = {
            "population": population,
            "median_household_income": col(_VARS["median_household_income"]),
            "working_age_share": _safe_share(pop_16plus, population),
            "employment_rate": _safe_share(employed, pop_16plus),
            "bachelors_or_higher_share": _safe_share(bachelors, edu_total),
            "healthcare_employment_share": _safe_share(health, employed),
        }
    logger.info(f"Bulk ACS ZCTA pull: {len(out)} ZCTAs (year {year}).")
    return out


def fetch_acs_zcta_bulk(
    year: int = DEFAULT_ACS_YEAR,
    *,
    api_key: str = CENSUS_API_KEY,
    cache: Optional[Cache] = None,
    ttl_seconds: int = CACHE_TTL_DEFAULT_SECONDS * 12,  # ~1 year
) -> Dict[str, Dict[str, Optional[float]]]:
    """Return ``{zip5: {signal: value|None}}`` for all ZCTAs, cached.

    Signals: population, median_household_income, working_age_share,
    employment_rate, bachelors_or_higher_share, healthcare_employment_share.
    ``population_density`` is added later by the build script (needs land area).
    """
    value, _as_of = cached_call(
        cache,
        provider="census",
        method="acs_zcta_bulk",
        params={"year": year, "keyed": bool(api_key)},
        ttl_seconds=ttl_seconds,
        live_call=lambda: _fetch_live(year, api_key),
    )
    return value or {}
