"""
ALLCPR Site Intelligence — web dashboard (FastAPI, Render-ready).

This is a thin read-only layer over pre-generated JSON. It does NOT run the
scoring pipeline, call paid APIs, or touch a database. Build the inputs
offline first::

    python scripts/generate_html_report.py          # latest_report.json
    python scripts/build_national_demand.py          # national_demand.json
    python scripts/enrich_top_zips.py ...            # national_demand_enriched.json (optional)
    python scripts/build_lite_outputs.py --details   # lite map + ZIP details
    python scripts/backtest_modeled_vs_historical.py # model_backtest.json (optional)

The only things computed at normal request time are cheap local annotations:
manual commercial-validation CSV (data/manual/commercial_validation.csv) by ZIP,
and offline API-candidate gating metadata. A separate on-click reverse-geocode
endpoint calls OpenStreetMap Nominatim only when the user clicks the map. No
live Places, no scoring pipeline. Then serve::

    uvicorn web_app:app --host 0.0.0.0 --port 8000       # local
    uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1  # Render

All paths are project-root relative (pathlib) so the app runs unchanged on
Render or any host.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.config import (
    DATA_DIR,
    PRODUCT_NAME,
    PRODUCT_STATUS,
    PRODUCT_VERSION,
    REQUEST_TIMEOUT,
)
from app.ops.routes import router as ops_router
from app.reports.commercial_validation import (
    COMMERCIAL_VALIDATION_FILE,
    load_commercial_summaries,
)
from app.reports.report_export import (
    LATEST_REPORT_PATH,
    MODEL_BACKTEST_PATH,
    PROCESSED_DIR,
    load_latest_report_json,
    load_model_backtest_json,
)
from app.scoring.api_candidate_filter import annotate_api_candidate
from app.scoring.historical_proven_demand import compute_proven_demand_score
from app.scoring.model_calibration import compare_modeled_vs_proven
from app.scoring.site_priority_score import annotate_site_priority_scores
from app.scoring.zip_modeled_opportunity import compute_zip_modeled_opportunity

ROOT = Path(__file__).resolve().parent
DASHBOARD_HTML = ROOT / "app" / "web" / "dashboard.html"
ZCTA_BOUNDARY_CANDIDATES = (
    DATA_DIR / "processed" / "zcta_boundaries_simplified.geojson",
    DATA_DIR / "processed" / "zcta_boundaries_simplified.json",
    ROOT / "static" / "zcta_boundaries_simplified.geojson",
    ROOT / "app" / "web" / "static" / "zcta_boundaries_simplified.geojson",
)
TIGERWEB_ZCTA_QUERY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/tigerWMS_Current/MapServer/2/query"
)
NATIONAL_DEMAND_QA_PATH = PROCESSED_DIR / "national_demand_qa.json"
NATIONAL_DEMAND_LITE_PATH = PROCESSED_DIR / "national_demand_lite.json"
NATIONAL_DEMAND_LITE_GZ_PATH = PROCESSED_DIR / "national_demand_lite.json.gz"
ZIP_DETAILS_DIR = PROCESSED_DIR / "zip_details"
ZIP_DETAILS_JSONL_PATH = PROCESSED_DIR / "zip_details.jsonl"
ZIP_DETAILS_INDEX_PATH = PROCESSED_DIR / "zip_details_index.json"

app = FastAPI(title="ALLCPR Site Intelligence Dashboard")
# The national modeled layer can be a few MB of JSON; compress it on the wire.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# --------------------------------------------------------------------------
# No site-wide auth (intentionally)
# --------------------------------------------------------------------------
# This is an internal tool served fully open — no login on any route. It exposes
# pre-generated JSON plus whatever ops-store snapshot the operator chooses to
# upload; there is deliberately no password gate. (A DASHBOARD_PASSWORD Basic-
# auth middleware previously lived here and was removed at the operator's
# request.) If this ever carries data that must not be public — or once live
# email is enabled, since the outreach send/tick endpoints would then be
# publicly triggerable — put it behind auth again (network allowlist, a
# reverse proxy, or restoring the middleware).
# Expansion-operations layer (instructor/space leads, operating readiness).
app.include_router(ops_router)

_MISSING_REPORT_ERROR = {
    "error": "latest_report_missing",
    "message": (
        "Run the pipeline or generate_html_report first to create "
        "data/processed/latest_report.json."
    ),
}

_MISSING_NATIONAL_ERROR = {
    "error": "national_demand_missing",
    "message": (
        "Run `python scripts/build_lite_outputs.py` to create "
        "data/processed/national_demand_lite.json.gz (offline modeled layer)."
    ),
}

_MISSING_ZIP_DETAIL_ERROR = {
    "error": "zip_detail_missing",
    "message": (
        "Run `python scripts/build_lite_outputs.py --details` to create "
        "data/processed/zip_details.jsonl and zip_details_index.json "
        "(or split files under data/processed/zip_details/{zip}.json(.gz))."
    ),
}

_MISSING_BACKTEST_ERROR = {
    "error": "model_backtest_missing",
    "message": (
        "Run `python scripts/backtest_modeled_vs_historical.py` to create "
        "data/processed/model_backtest.json."
    ),
}

_REVERSE_GEOCODE_CACHE: Dict[str, Dict[str, Any]] = {}
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def _merge_commercial(rows: List[Dict[str, Any]]) -> int:
    """Attach the manual commercial-validation summary to matching ZIP rows.

    In place; returns the match count. Cheap (small CSV, single pass) and the
    only request-time computation in the app. Missing CSV → no-op.
    """
    summaries = load_commercial_summaries(COMMERCIAL_VALIDATION_FILE)
    if not summaries:
        return 0
    matched = 0
    for row in rows:
        summary = summaries.get(str(row.get("zip")))
        if summary:
            row["commercial"] = summary
            matched += 1
    return matched


def _annotate_api_candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach offline API-budget gating metadata to national ZIP rows."""
    return [annotate_api_candidate(row) for row in rows]


def _extend_risk_flags(row: Dict[str, Any], *flags: str) -> None:
    existing = row.get("risk_flags") or []
    if isinstance(existing, str):
        existing = [existing]
    seen = {str(flag) for flag in existing if flag}
    out = [str(flag) for flag in existing if flag]
    for flag in flags:
        if flag and flag not in seen:
            out.append(flag)
            seen.add(flag)
    row["risk_flags"] = out


def _modeled_features_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct scoring features for older generated national JSON files."""
    return {
        "population": row.get("population"),
        "population_density": row.get("population_density"),
        "median_household_income": (
            row.get("median_household_income") or row.get("median_income")
        ),
        "working_age_share": row.get("working_age_share"),
        "employment_rate": row.get("employment_rate"),
        "bachelors_or_higher_share": row.get("bachelors_or_higher_share"),
        "healthcare_employment_share": row.get("healthcare_employment_share"),
        "healthcare_facility_density": row.get("healthcare_facility_density"),
        "community_facility_density": row.get("community_facility_density"),
        "training_school_density": row.get("training_school_density"),
        "competition_gap_score": row.get("competition_gap_score"),
    }


def _annotate_modeled_explanations(rows: List[Dict[str, Any]]) -> None:
    """Backfill explainability fields when local national JSON predates them."""
    fields = (
        "score_drivers",
        "score_weaknesses",
        "plain_english_summary",
        "recommended_next_action",
        "risk_flags",
    )
    for row in rows:
        if all(row.get(field) is not None for field in fields):
            continue
        scored = compute_zip_modeled_opportunity(_modeled_features_from_row(row))
        for field in fields:
            if row.get(field) is None:
                row[field] = scored.get(field)


def _zip_detail_paths(zip_code: str) -> tuple[Path, Path]:
    safe_zip = str(zip_code).strip()
    return ZIP_DETAILS_DIR / f"{safe_zip}.json.gz", ZIP_DETAILS_DIR / f"{safe_zip}.json"


def _load_zip_detail(zip_code: str) -> Optional[Dict[str, Any]]:
    """Load one modeled ZIP detail file without touching the national payload."""
    gz_path, json_path = _zip_detail_paths(zip_code)
    try:
        if gz_path.exists():
            with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        if json_path.exists():
            return json.loads(json_path.read_text(encoding="utf-8"))
        if ZIP_DETAILS_JSONL_PATH.exists() and ZIP_DETAILS_INDEX_PATH.exists():
            index = json.loads(ZIP_DETAILS_INDEX_PATH.read_text(encoding="utf-8"))
            offset = index.get(str(zip_code))
            if offset is None:
                return None
            with ZIP_DETAILS_JSONL_PATH.open("rb") as fh:
                fh.seek(int(offset))
                line = fh.readline()
            if line:
                return json.loads(line.decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _modeled_details_by_zip(zip_codes: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """Return any pre-split modeled detail rows for a small ZIP set."""
    out: Dict[str, Dict[str, Any]] = {}
    for raw_zip in zip_codes:
        zip_code = str(raw_zip or "").strip()
        if not zip_code or zip_code in out:
            continue
        row = _load_zip_detail(zip_code)
        if row:
            out[zip_code] = row
    return out


def _historical_by_zip() -> Dict[str, Dict[str, Any]]:
    try:
        data = load_latest_report_json(LATEST_REPORT_PATH)
    except FileNotFoundError:
        return {}
    return {str(row.get("zip")): row for row in data.get("zip_demand") or []
            if row.get("zip")}


def _annotate_historical_validation(
    rows: List[Dict[str, Any]],
    modeled: Dict[str, Dict[str, Any]] | None = None,
) -> None:
    """Attach proven-demand and calibration fields to real historical rows."""
    if modeled is None:
        modeled = _modeled_details_by_zip(row.get("zip") for row in rows)
    for row in rows:
        row.update(compute_proven_demand_score(row))
        row["historical_status"] = "has_allcpr_history"
        modeled_row = modeled.get(str(row.get("zip")))
        if modeled_row:
            row.update(compare_modeled_vs_proven(modeled_row, row))


def _annotate_modeled_history_status(rows: List[Dict[str, Any]]) -> None:
    """Attach history status/calibration only where real ALLCPR history exists."""
    historical = _historical_by_zip()
    for row in rows:
        hist = historical.get(str(row.get("zip")))
        if not hist:
            row["historical_status"] = "no_allcpr_history"
            _extend_risk_flags(row, "no_allcpr_history")
            continue
        row.update(compare_modeled_vs_proven(row, hist))
        row["historical_status"] = "has_allcpr_history"
        agreement = row.get("model_agreement")
        if agreement in {
            "model_overpredicts",
            "model_underpredicts",
            "hidden_opportunity",
            "insufficient_history",
        }:
            _extend_risk_flags(row, str(agreement))


def _reverse_cache_key(lat: float, lng: float) -> str:
    return f"{lat:.5f},{lng:.5f}"


def _reverse_geocode_osm(lat: float, lng: float) -> Dict[str, Any]:
    """Reverse-geocode one clicked map point via OSM Nominatim, with memory cache."""
    key = _reverse_cache_key(lat, lng)
    if key in _REVERSE_GEOCODE_CACHE:
        return _REVERSE_GEOCODE_CACHE[key]
    resp = requests.get(
        _NOMINATIM_REVERSE_URL,
        params={
            "format": "jsonv2",
            "lat": f"{lat:.7f}",
            "lon": f"{lng:.7f}",
            "addressdetails": 1,
            "zoom": 18,
        },
        headers={"User-Agent": "ALLCPR Site Intelligence Dashboard"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    address = data.get("address") or {}
    result = {
        "lat": lat,
        "lng": lng,
        "zip": address.get("postcode") or "",
        "address": data.get("display_name") or "",
        "source": "OpenStreetMap Nominatim",
    }
    _REVERSE_GEOCODE_CACHE[key] = result
    return result


@app.get("/health")
def health() -> dict:
    """Render health check."""
    return {
        "status": "ok",
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "product_status": PRODUCT_STATUS,
    }


@app.get("/api/report")
def api_report() -> JSONResponse:
    """Return the full pre-generated dashboard payload (+ commercial merge)."""
    try:
        data = load_latest_report_json(LATEST_REPORT_PATH)
    except FileNotFoundError:
        return JSONResponse(_MISSING_REPORT_ERROR, status_code=404)
    rows = data.get("zip_demand") or []
    modeled = _modeled_details_by_zip(row.get("zip") for row in rows)
    _annotate_historical_validation(rows, modeled)
    _merge_commercial(data.get("zip_demand") or [])
    _merge_commercial(data.get("candidates") or [])
    return JSONResponse(data, headers=_cache_control(60))


@app.get("/api/zip-demand")
def api_zip_demand() -> JSONResponse:
    """Return only the ZIP demand rows (+ commercial merge)."""
    try:
        data = load_latest_report_json(LATEST_REPORT_PATH)
    except FileNotFoundError:
        return JSONResponse(_MISSING_REPORT_ERROR, status_code=404)
    rows = data.get("zip_demand") or []
    modeled = _modeled_details_by_zip(row.get("zip") for row in rows)
    _annotate_historical_validation(rows, modeled)
    _merge_commercial(rows)
    return JSONResponse(rows)


def _file_signature(path: Path) -> tuple:
    """Identity of a file for cache invalidation: (path, mtime, size)."""
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _cache_control(max_age: int, etag: Optional[str] = None) -> Dict[str, str]:
    """Browser/CDN cache headers for large static-ish JSON. ``max_age`` is short
    so a rebuilt (or mid-enrichment) file is revalidated quickly; the ETag lets
    the browser get a cheap 304 instead of redownloading multi-MB payloads."""
    headers = {"Cache-Control": f"public, max-age={max_age}"}
    if etag:
        headers["ETag"] = etag
    return headers


def _etag_for(signature: Any) -> str:
    """Stable, quote-wrapped ETag derived from a payload signature."""
    digest = hashlib.sha1(repr(signature).encode("utf-8")).hexdigest()[:16]
    return f'"{digest}"'


@app.get("/api/national-demand")
def api_national_demand(request: Request) -> Response:
    """Serve the prebuilt lightweight modeled map layer as gzipped bytes."""
    path = NATIONAL_DEMAND_LITE_GZ_PATH
    if not path.exists():
        return JSONResponse(_MISSING_NATIONAL_ERROR, status_code=404)
    signature = _file_signature(path)
    etag = _etag_for(signature)
    # Cheap revalidation: identical ETag means the browser already has the bytes.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=_cache_control(3600, etag))
    headers = _cache_control(3600, etag)
    headers["Content-Encoding"] = "gzip"
    return FileResponse(
        path,
        media_type="application/json",
        headers=headers,
    )


@app.get("/api/zip-demand/{zip_code}")
def api_zip_demand_detail(zip_code: str) -> JSONResponse:
    """Return full modeled details for exactly one ZIP."""
    zip_code = str(zip_code).strip()
    if not (len(zip_code) == 5 and zip_code.isdigit()):
        return JSONResponse(
            {
                "error": "invalid_zip",
                "message": "ZIP must be a 5-digit code.",
            },
            status_code=400,
        )
    row = _load_zip_detail(zip_code)
    if not row:
        err = dict(_MISSING_ZIP_DETAIL_ERROR)
        err["message"] = err["message"].format(zip=zip_code)
        return JSONResponse(err, status_code=404)
    rows = [row]
    _annotate_modeled_explanations(rows)
    _merge_commercial(rows)
    _annotate_modeled_history_status(rows)
    row = _annotate_api_candidates(rows)[0]
    row = annotate_site_priority_scores(row)
    return JSONResponse(row, headers=_cache_control(3600))


@app.get("/api/model-backtest")
def api_model_backtest() -> JSONResponse:
    """Return the modeled-vs-historical backtest, or 404 with a build hint."""
    try:
        data = load_model_backtest_json(MODEL_BACKTEST_PATH)
    except FileNotFoundError:
        return JSONResponse(_MISSING_BACKTEST_ERROR, status_code=404)
    return JSONResponse(data)


@app.get("/api/national-demand-qa")
def api_national_demand_qa() -> JSONResponse:
    """Return the generated national-demand QA JSON if present."""
    try:
        data = json.loads(NATIONAL_DEMAND_QA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": "national_demand_qa_missing",
                "message": (
                    "Run `python3 scripts/qa_national_demand.py` to create "
                    "data/processed/national_demand_qa.json."
                ),
            },
            status_code=404,
        )
    except json.JSONDecodeError as exc:
        return JSONResponse(
            {
                "error": "national_demand_qa_invalid",
                "message": str(exc),
            },
            status_code=500,
        )
    return JSONResponse(data)


@app.get("/api/zcta-boundaries")
def api_zcta_boundaries(
    min_lng: Optional[float] = None,
    min_lat: Optional[float] = None,
    max_lng: Optional[float] = None,
    max_lat: Optional[float] = None,
):
    """Serve optional simplified ZCTA polygon GeoJSON, never required.

    With bbox query params, proxy a small viewport slice from Census TIGERweb so
    the browser never downloads massive full-US polygons.
    """
    bbox_values = (min_lng, min_lat, max_lng, max_lat)
    has_bbox = all(v is not None for v in bbox_values)
    if has_bbox:
        width = float(max_lng) - float(min_lng)
        height = float(max_lat) - float(min_lat)
        if width <= 0 or height <= 0:
            return JSONResponse(
                {"error": "zcta_boundaries_bad_bbox",
                 "message": "ZIP boundary viewport is invalid."},
                status_code=400,
            )
        if width * height > 25:
            return JSONResponse(
                {"error": "zcta_boundaries_zoom_in",
                 "message": "Zoom in to load ZIP boundary polygons."},
                status_code=400,
            )
        try:
            resp = requests.get(
                TIGERWEB_ZCTA_QUERY_URL,
                params={
                    "f": "geojson",
                    "where": "1=1",
                    "outFields": "ZCTA5,GEOID,BASENAME",
                    "returnGeometry": "true",
                    "geometry": json.dumps({
                        "xmin": min_lng,
                        "ymin": min_lat,
                        "xmax": max_lng,
                        "ymax": max_lat,
                        "spatialReference": {"wkid": 4326},
                    }),
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "outSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "geometryPrecision": "5",
                    "resultRecordCount": "2000",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(data["error"].get("message") or data["error"])
            features = data.get("features") or []
            return JSONResponse({
                "type": "FeatureCollection",
                "features": features,
                "source": "US Census TIGERweb 2020 ZCTA",
            }, headers=_cache_control(86400))
        except Exception as exc:
            for path in ZCTA_BOUNDARY_CANDIDATES:
                if path.exists():
                    return FileResponse(path, media_type="application/geo+json",
                                        headers=_cache_control(86400))
            return JSONResponse(
                {
                    "error": "zcta_boundaries_unavailable",
                    "message": (
                        "ZIP boundary data is not loaded yet. The live Census "
                        f"TIGERweb boundary request failed: {exc}"
                    ),
                },
                status_code=503,
            )

    for path in ZCTA_BOUNDARY_CANDIDATES:
        if path.exists():
            return FileResponse(path, media_type="application/geo+json",
                                headers=_cache_control(86400))
    searched = []
    for path in ZCTA_BOUNDARY_CANDIDATES:
        try:
            searched.append(str(path.relative_to(ROOT)))
        except ValueError:
            searched.append(str(path))
    return JSONResponse(
        {
            "error": "zcta_boundaries_missing",
            "message": (
                "ZIP boundary data is not loaded yet. Add a "
                "simplified GeoJSON at data/processed/zcta_boundaries_simplified.geojson "
                "or use ZIP points / smooth heat."
            ),
            "searched": searched,
        },
        status_code=404,
    )


@app.get("/api/reverse-geocode")
def api_reverse_geocode(lat: float, lng: float) -> JSONResponse:
    """Return ZIP/address for one clicked map point. On-demand only."""
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JSONResponse(
            {"error": "invalid_coordinates",
             "message": "lat must be -90..90 and lng must be -180..180."},
            status_code=400,
        )
    try:
        return JSONResponse(_reverse_geocode_osm(lat, lng))
    except (requests.RequestException, ValueError) as exc:
        return JSONResponse(
            {"error": "reverse_geocode_failed",
             "message": str(exc)[:300],
             "lat": lat,
             "lng": lng},
            status_code=502,
        )


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve the single-page dashboard."""
    if not DASHBOARD_HTML.exists():
        return HTMLResponse(
            "<h1>Dashboard template missing</h1>"
            f"<p>Expected {DASHBOARD_HTML}</p>",
            status_code=500,
        )
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))
