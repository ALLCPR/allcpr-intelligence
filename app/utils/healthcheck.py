"""
API health-check.

Pings each configured external API with one cheap call and reports
green / red / skipped (not configured). The point is to catch silent
breakage — Foursquare deprecating an endpoint (HTTP 410), Adzuna changing
its ``where`` format (HTTP 503), a rotated key, a category-ID change — BEFORE
it quietly zeroes out a data source in the middle of a report you're trusting.

Each check returns a ``CheckResult``: status is one of
``"ok" | "down" | "skipped"``. ``skipped`` means the API isn't configured
(no key), which is not a failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple

import requests

from app.config import CENSUS_API_KEY, GOOGLE_MAPS_API_KEY, REQUEST_TIMEOUT

# A central, cheap probe coordinate (San Francisco City Hall).
_PROBE_LAT, _PROBE_LON = 37.7793, -122.4193


@dataclass
class CheckResult:
    name: str
    status: str          # "ok" | "down" | "skipped"
    detail: str

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "down": "✗", "skipped": "–"}.get(self.status, "?")


def _safe(name: str, fn: Callable[[], Tuple[str, str]]) -> CheckResult:
    """Run a check fn returning (status, detail); never raise."""
    try:
        status, detail = fn()
        return CheckResult(name=name, status=status, detail=detail)
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult(name=name, status="down",
                           detail=f"{exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def _check_google() -> Tuple[str, str]:
    if not GOOGLE_MAPS_API_KEY:
        return "skipped", "GOOGLE_MAPS_API_KEY not set"
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    r = requests.get(url, params={"address": "San Francisco, CA",
                                  "key": GOOGLE_MAPS_API_KEY},
                     timeout=REQUEST_TIMEOUT)
    data = r.json() if r.status_code == 200 else {}
    status = data.get("status")
    if status == "OK":
        return "ok", "geocode OK"
    if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
        return "down", f"Google status={status} ({data.get('error_message','')[:80]})"
    return "down", f"HTTP {r.status_code} status={status}"


def _check_census() -> Tuple[str, str]:
    # Mirror the real collector's query shape (NAME + a core var, county
    # geography, key when present). A bare state query can return non-JSON.
    url = "https://api.census.gov/data/2022/acs/acs5"
    params = {
        "get": "NAME,B01003_001E",
        "for": "county:075",   # San Francisco County
        "in": "state:06",
    }
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return "down", f"HTTP {r.status_code}: {(r.text or '')[:80]}"
    try:
        data = r.json()
    except ValueError:
        return "down", f"non-JSON body: {(r.text or '')[:80]}"
    if isinstance(data, list) and len(data) >= 2:
        return "ok", "ACS responding"
    return "down", "unexpected ACS response shape"


def _check_foursquare() -> Tuple[str, str]:
    from app.collectors import foursquare_places as fsq
    if not fsq.is_configured():
        return "skipped", "FOURSQUARE_API_KEY not set"
    results = fsq.search_commercial_anchors(
        origin=(_PROBE_LAT, _PROBE_LON), radius_miles=0.5,
    )
    if results:
        return "ok", f"{len(results)} anchors returned"
    # Could be a real empty area, but at the SF center it means auth/endpoint
    # breakage far more often than a genuinely empty result.
    return "down", "0 anchors at SF center (likely auth/endpoint/category break)"


def _check_yelp() -> Tuple[str, str]:
    from app.collectors import yelp_competitors as yelp
    if not yelp.is_configured():
        return "skipped", "YELP_API_KEY not set"
    records = yelp.fetch_yelp_competitors(
        origin=(_PROBE_LAT, _PROBE_LON), radius_miles=2.0,
    )
    if records:
        return "ok", f"{len(records)} competitors returned"
    return "down", "0 competitors at SF center (likely auth break)"


def _check_adzuna() -> Tuple[str, str]:
    from app.collectors import adzuna_jobs as adz
    if not adz.is_configured():
        return "skipped", "ADZUNA_APP_ID / ADZUNA_APP_KEY not set"
    raw = adz._adzuna_request(
        bucket_label="healthcheck", what_or="registered nurse",
        where="San Francisco, CA", radius_miles=5, results_per_page=1,
    )
    if raw:
        return "ok", f"{len(raw)} posting(s) returned"
    return "down", "0 postings for SF nurse search (likely auth/where/quota)"


def _check_ors() -> Tuple[str, str]:
    from app.collectors import openrouteservice_isochrones as ors
    if not ors.is_configured():
        return "skipped", "ORS_API_KEY not set"
    poly = ors.fetch_isochrone((_PROBE_LAT, _PROBE_LON), minutes=5)
    if poly and len(poly) >= 3:
        return "ok", f"isochrone polygon ({len(poly)} pts)"
    return "down", "no isochrone polygon (likely auth/quota)"


def _check_mapbox() -> Tuple[str, str]:
    from app.collectors import mapbox_isochrones as mbx
    if not mbx.is_configured():
        return "skipped", "MAPBOX_TOKEN not set"
    poly = mbx.fetch_isochrone((_PROBE_LAT, _PROBE_LON), minutes=5)
    if poly and len(poly) >= 3:
        return "ok", f"isochrone polygon ({len(poly)} pts)"
    return "down", "no isochrone polygon (likely auth/quota)"


def _check_bls() -> Tuple[str, str]:
    # BLS QCEW is free/no-key; verify the CSV endpoint serves a known county.
    from app.collectors import bls_or_labor as bls
    out = bls.collect_labor(_PROBE_LAT, _PROBE_LON)
    vals = out.get("values") or {}
    if vals.get("healthcare_employment_count"):
        return "ok", (f"SF healthcare employment "
                      f"{vals['healthcare_employment_count']:,}")
    return "down", "QCEW returned no healthcare employment (endpoint/format?)"


def _check_llm() -> Tuple[str, str]:
    from app.reports import ai_summary
    provider = ai_summary.resolve_provider()
    if provider is None:
        return "skipped", "no GROQ_API_KEY / OPENAI_API_KEY set"
    out = ai_summary._chat(provider, [
        {"role": "user", "content": "Reply with the single word: ok"},
    ])
    if out:
        return "ok", f"{provider} responding"
    return "down", f"{provider} key set but no response (auth/quota/model?)"


def _check_overpass() -> Tuple[str, str]:
    from app.collectors import osm_zoning
    if not osm_zoning.is_available():
        return "skipped", "OSM_ZONING_ENABLED=false"
    # Tiny bbox around SF City Hall.
    polys = osm_zoning.fetch_commercial_polygons(
        (37.778, -122.420, 37.781, -122.417),
    )
    # Empty is plausible for a tiny bbox; treat a non-exception as "ok".
    return "ok", f"{len(polys)} commercial polygon(s) in probe bbox"


def _check_zip_centroids() -> Tuple[str, str]:
    # Local-data check (not an API): does zip_centroids.csv cover the ZIPs that
    # actually carry held-class demand? An uncovered demand ZIP is silently
    # dropped from radius matching — exactly the kind of quiet degradation this
    # tool exists to surface.
    from app.collectors import enrollware
    from app.scoring import zip_demand
    centroids = zip_demand.load_zip_centroids()
    records = enrollware.load_records()
    if not records:
        if centroids:
            return "ok", f"{len(centroids)} centroid(s); no enrollware export to audit"
        return "skipped", "no centroid file and no enrollware export"
    coverage = zip_demand.audit_centroid_coverage(
        zip_demand.aggregate_zip_demand(records), centroids)
    if not centroids:
        return "skipped", coverage.summary()
    if coverage.total_demand_zips == 0:
        return "ok", coverage.summary()
    if coverage.missing_zips:
        return "down", coverage.summary()
    return "ok", coverage.summary()


CHECKS: List[Tuple[str, Callable[[], Tuple[str, str]]]] = [
    ("Google Maps", _check_google),
    ("Census ACS", _check_census),
    ("BLS QCEW", _check_bls),
    ("OpenStreetMap/Overpass", _check_overpass),
    ("Foursquare", _check_foursquare),
    ("Yelp Fusion", _check_yelp),
    ("Adzuna", _check_adzuna),
    ("OpenRouteService", _check_ors),
    ("Mapbox", _check_mapbox),
    ("LLM (OpenAI/Groq)", _check_llm),
    ("ZIP centroids", _check_zip_centroids),
]


def run_all() -> List[CheckResult]:
    return [_safe(name, fn) for name, fn in CHECKS]


def format_report(results: List[CheckResult]) -> str:
    lines = ["API health check", "=" * 40]
    width = max(len(r.name) for r in results)
    for r in results:
        lines.append(f"  {r.symbol}  {r.name.ljust(width)}  "
                     f"[{r.status}]  {r.detail}")
    down = [r for r in results if r.status == "down"]
    ok = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skipped"]
    lines.append("=" * 40)
    lines.append(f"  {len(ok)} ok · {len(down)} down · {len(skipped)} skipped")
    return "\n".join(lines)


def any_down(results: List[CheckResult]) -> bool:
    return any(r.status == "down" for r in results)
