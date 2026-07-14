"""Tests for the FastAPI dashboard (web_app.py)."""
from __future__ import annotations

import json
import gzip

import pytest
from fastapi.testclient import TestClient

import web_app
from app.reports import report_export as rx


@pytest.fixture
def client():
    return TestClient(web_app.app)


def _write_report(path):
    payload = {
        "context": {
            "mode": "city", "cities": ["San Jose, CA"],
            "zip_demand_report": {
                "total_zips": 1, "total_classes": 41,
                "rows": [{
                    "zip": "95112", "demand_score": 88.8, "classes": 41,
                    "arc_cpr_students": 294, "arc_bls_students": 127,
                    "aha_bls_students": 0, "avg_students": 10.3,
                    "fill_rate": 85.4, "centroid_present": True,
                    "lat": 37.33, "lng": -121.88, "total_students": 421,
                }],
            },
        },
        "report_interpretation": {
            "executive_verdict": {"best_candidate": "Test area"},
            "next_actions": ["Validate rent."],
        },
        "candidates": [],
    }
    return rx.write_latest_report_json(payload, output_path=path)


def _write_gzip_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(payload, fh)


# --------------------------------------------------------------------------- #
# With a report present
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_optional_layers(tmp_path, monkeypatch):
    """Keep the real on-disk enriched/commercial files from leaking into tests."""
    monkeypatch.setattr(web_app, "COMMERCIAL_VALIDATION_FILE",
                        tmp_path / "no_commercial.csv")
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_LITE_PATH",
                        tmp_path / "national_demand_lite.json")
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_LITE_GZ_PATH",
                        tmp_path / "national_demand_lite.json.gz")
    monkeypatch.setattr(web_app, "ZIP_DETAILS_DIR",
                        tmp_path / "zip_details")
    monkeypatch.setattr(web_app, "ZIP_DETAILS_JSONL_PATH",
                        tmp_path / "zip_details.jsonl")
    monkeypatch.setattr(web_app, "ZIP_DETAILS_INDEX_PATH",
                        tmp_path / "zip_details_index.json")


@pytest.fixture
def with_report(tmp_path, monkeypatch):
    path = tmp_path / "latest_report.json"
    _write_report(path)
    monkeypatch.setattr(web_app, "LATEST_REPORT_PATH", path)
    return path


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["product"] == "ALLCPR Site Intelligence"
    # Assert against the config constant so the test tracks version bumps.
    from app.config import PRODUCT_VERSION
    assert data["version"] == PRODUCT_VERSION
    assert data["product_status"] == "Internal decision-support product"


def test_api_report_returns_json(client, with_report):
    resp = client.get("/api/report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "city"
    assert "executive_summary" in data
    assert isinstance(data["zip_demand"], list)


def test_api_zip_demand_returns_list(client, with_report):
    resp = client.get("/api/zip-demand")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert rows[0]["zip"] == "95112"


def test_root_returns_html_with_required_elements(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "zip-input" in body                 # ZIP search input
    # Course selector with all four modes.
    for label in ("Overall", "AHA BLS", "ARC BLS", "ARC CPR"):
        assert label in body
    assert 'id="map"' in body                  # map container
    assert "/api/report" in body               # references the historical API
    # Two-layer dashboard: layer selector + national modeled layer.
    assert "Historical ALLCPR" in body
    assert "Modeled national" in body
    assert "Street" in body
    assert "Satellite" in body
    assert "setBaseMap" in body
    assert "ZIP points" in body
    assert "Smooth heat" in body
    assert "Both" in body
    assert "ZIP boundaries" in body
    assert "Light" in body
    assert "Normal" in body
    assert "Strong" in body
    assert "setVizMode" in body
    assert "setHeatIntensity" in body
    assert "viz-seg" in body
    assert "heat-intensity-seg" in body
    assert "Smooth heat = regional intensity, not exact ZIP boundaries." in body
    assert "ZIP detail panel = exact ZIP-level evidence." in body
    assert "renderSmoothHeat" in body
    assert "HEAT_INTENSITY" in body
    assert "heatOpacity" in body
    assert "applyHeatOpacity" in body
    assert "/api/national-demand" in body
    assert "/api/zip-demand/${encodeURIComponent(key)}" in body
    assert "/api/zcta-boundaries" in body
    assert "/api/national-demand-qa" in body
    assert "leaflet-heat" in body              # heat layer for the national map


# --------------------------------------------------------------------------- #
# Missing report file → helpful error
# --------------------------------------------------------------------------- #
@pytest.fixture
def without_report(tmp_path, monkeypatch):
    path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(web_app, "LATEST_REPORT_PATH", path)
    return path


def test_api_report_missing_file(client, without_report):
    resp = client.get("/api/report")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "latest_report_missing"
    assert "generate_html_report" in data["message"]


def test_api_zip_demand_missing_file(client, without_report):
    resp = client.get("/api/zip-demand")
    assert resp.status_code == 404
    assert resp.json()["error"] == "latest_report_missing"


# --------------------------------------------------------------------------- #
# National modeled-demand endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture
def with_national(tmp_path, monkeypatch):
    lite_path = tmp_path / "national_demand_lite.json"
    gz_path = tmp_path / "national_demand_lite.json.gz"
    detail_dir = tmp_path / "zip_details"
    lite_payload = {
        "layer": "modeled_national_demand", "tier": "baseline",
        "acs_vintage": 2022, "zip_count": 1,
        "methodology": "Modeled estimate from public data.",
        "rows": [{"zip": "95112", "lat": 37.33, "lon": -121.88,
                  "overall_score": 74.9, "aha_bls": 78.1,
                  "arc_bls": 78.1, "arc_cpr": 71.8,
                  "tier": "baseline", "data_confidence": "ok"}],
    }
    detail_payload = {
        "zip": "95112", "lat": 37.33, "lng": -121.88,
        "overall": 74.9, "bls_demand": 78.1, "cpr_demand": 71.8,
        "tier": "baseline", "recommendation": "Promising",
        "data_confidence": "ok", "population": 55000,
        "population_density": 8000,
        "healthcare_employment_share": 0.01,
        "healthcare_facility_count": 30,
        "training_school_count": 18,
        "community_facility_count": 22,
        "competitor_count": 23,
        "historical_course_mix": {
            "aha_bls": 0.15,
            "arc_bls": 0.20,
            "arc_cpr": 0.65,
        },
        "best_historical_course": "ARC CPR",
    }
    lite_path.write_text(json.dumps(lite_payload), encoding="utf-8")
    _write_gzip_json(gz_path, lite_payload)
    detail_dir.mkdir()
    (detail_dir / "95112.json").write_text(json.dumps(detail_payload), encoding="utf-8")
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_LITE_PATH", lite_path)
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_LITE_GZ_PATH", gz_path)
    monkeypatch.setattr(web_app, "ZIP_DETAILS_DIR", detail_dir)
    monkeypatch.setattr(web_app, "LATEST_REPORT_PATH", tmp_path / "no_history.json")
    return gz_path


def test_api_national_demand_returns_lite_gzip(client, with_national):
    resp = client.get("/api/national-demand")
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    data = resp.json()
    assert data["layer"] == "modeled_national_demand"
    assert data["tier"] == "baseline"
    assert data["rows"][0]["zip"] == "95112"
    assert data["rows"][0]["lon"] == -121.88
    assert data["rows"][0]["overall_score"] == 74.9
    assert "population" not in data["rows"][0]
    assert "score_drivers" not in data["rows"][0]
    assert "features" not in data
    assert "geometry" not in data["rows"][0]


def test_api_national_demand_missing_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_LITE_GZ_PATH",
                        tmp_path / "nope.json.gz")
    resp = client.get("/api/national-demand")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "national_demand_missing"
    assert "build_lite_outputs" in data["message"]


def test_api_national_demand_uses_lite_gz_file(client, with_national):
    first = client.get("/api/national-demand").json()
    assert [r["zip"] for r in first["rows"]] == ["95112"]

    payload = {
        "layer": "modeled_national_demand",
        "tier": "lite",
        "rows": [{"zip": "90001", "lat": 34.0, "lon": -118.2,
                  "overall_score": 50.0, "aha_bls": 50.0,
                  "arc_bls": 50.0, "arc_cpr": 50.0}],
    }
    _write_gzip_json(with_national, payload)
    refreshed = client.get("/api/national-demand").json()
    assert [r["zip"] for r in refreshed["rows"]] == ["90001"]


def test_api_national_demand_sets_cache_headers_and_etag(client, with_national):
    """The big modeled payload advertises cache headers and a stable ETag."""
    resp = client.get("/api/national-demand")
    assert resp.status_code == 200
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.headers.get("content-encoding") == "gzip"
    assert resp.headers.get("etag")


def test_api_national_demand_etag_revalidates_with_304(client, with_national):
    """A matching If-None-Match yields a 304 with no body re-download."""
    first = client.get("/api/national-demand")
    etag = first.headers["etag"]
    second = client.get("/api/national-demand", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert not second.content
    assert second.headers.get("etag") == etag


def test_api_zip_demand_detail_returns_full_one_zip(client, with_national):
    resp = client.get("/api/zip-demand/95112")
    assert resp.status_code == 200
    data = resp.json()
    assert data["zip"] == "95112"
    assert data["population"] == 55000
    assert data["overall"] == 74.9
    assert data["score_formula_version"] == "v2.1"
    assert "market_demand_score" in data
    assert "validation_evidence_score" in data
    assert "final_site_priority_score" in data
    assert "site_priority_decision" in data
    assert data["competition_risk_label"] == "Saturated unless differentiated"
    assert set(data["course_priority_profiles"]) == {
        "overall", "aha_bls", "arc_bls", "arc_cpr",
    }
    assert data["course_priority_profiles"]["arc_cpr"]["selected_course_label"] == "ARC CPR"
    assert "Community CPR demand" in " ".join(
        data["course_priority_profiles"]["arc_cpr"]["course_why_bullets"]
    )
    assert "Healthcare/BLS workforce" in " ".join(
        data["course_priority_profiles"]["aha_bls"]["course_why_bullets"]
    )
    assert "Healthcare and workplace" in " ".join(
        data["course_priority_profiles"]["arc_bls"]["course_why_bullets"]
    )
    assert "blended market view" in " ".join(
        data["course_priority_profiles"]["overall"]["course_why_bullets"]
    ).lower()
    breakdown = data["site_priority_score_breakdown"]
    assert {c["key"] for c in breakdown["components"]} == {
        "market_demand", "validation_evidence",
        "commercial_feasibility", "historical_fit",
    }
    if data["final_site_priority_score"] is not None:
        reconstructed = round(
            breakdown["subtotal"] - breakdown["competition_saturation_penalty"], 1
        )
        assert reconstructed == data["final_site_priority_score"]
    assert data["api_priority"] in {"low", "medium", "high", "finalist"}
    assert "plain_english_summary" in data


def test_api_zip_demand_detail_missing(client, with_national):
    resp = client.get("/api/zip-demand/99999")
    assert resp.status_code == 404
    assert resp.json()["error"] == "zip_detail_missing"


def test_app_startup_does_not_rebuild_or_enrich_national_data():
    assert web_app.app.router.on_startup == []
    assert not hasattr(web_app, "_NATIONAL_PAYLOAD_CACHE")


def test_api_report_sets_cache_headers(client):
    resp = client.get("/api/report")
    assert resp.status_code == 200
    assert "max-age" in resp.headers.get("cache-control", "")


# --------------------------------------------------------------------------- #
# Commercial-validation merge
# --------------------------------------------------------------------------- #
def test_commercial_merged_into_report(client, with_report, tmp_path, monkeypatch):
    csv = tmp_path / "commercial.csv"
    csv.write_text(
        "zip,address,monthly_rent,parking,available,classroom_fit,source_url,updated_at\n"
        "95112,1 A St,3000,Yes,Yes,Good,https://x/1,2026-06-11\n",
        encoding="utf-8")
    monkeypatch.setattr(web_app, "COMMERCIAL_VALIDATION_FILE", csv)
    rows = client.get("/api/report").json()["zip_demand"]
    row = next(r for r in rows if r["zip"] == "95112")
    assert row["commercial"]["commercial_validated"] is True
    assert row["commercial"]["commercial_ready"] is True


def test_no_commercial_csv_does_not_crash(client, with_report):
    # autouse fixture points COMMERCIAL_VALIDATION_FILE at a nonexistent path.
    rows = client.get("/api/report").json()["zip_demand"]
    assert "commercial" not in rows[0]


# --------------------------------------------------------------------------- #
# Model-backtest endpoint
# --------------------------------------------------------------------------- #
def test_api_model_backtest_returns_json(client, tmp_path, monkeypatch):
    path = tmp_path / "model_backtest.json"
    path.write_text(json.dumps({"sample_size": 5, "metrics": {}, "notes": ["x"]}),
                    encoding="utf-8")
    monkeypatch.setattr(web_app, "MODEL_BACKTEST_PATH", path)
    data = client.get("/api/model-backtest").json()
    assert data["sample_size"] == 5


def test_api_model_backtest_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "MODEL_BACKTEST_PATH", tmp_path / "nope.json")
    resp = client.get("/api/model-backtest")
    assert resp.status_code == 404
    assert resp.json()["error"] == "model_backtest_missing"


# --------------------------------------------------------------------------- #
# Optional QA and boundary endpoints
# --------------------------------------------------------------------------- #
def test_api_national_demand_qa_returns_json(client, tmp_path, monkeypatch):
    path = tmp_path / "national_demand_qa.json"
    path.write_text(json.dumps({"product": {"version": "v1.0.0"}, "ok": True}),
                    encoding="utf-8")
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_QA_PATH", path)
    resp = client.get("/api/national-demand-qa")
    assert resp.status_code == 200
    assert resp.json()["product"]["version"] == "v1.0.0"


def test_api_national_demand_qa_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "NATIONAL_DEMAND_QA_PATH", tmp_path / "missing.json")
    resp = client.get("/api/national-demand-qa")
    assert resp.status_code == 404
    assert resp.json()["error"] == "national_demand_qa_missing"


def test_api_zcta_boundaries_missing_is_clear(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "ZCTA_BOUNDARY_CANDIDATES",
                        (tmp_path / "missing.geojson",))
    resp = client.get("/api/zcta-boundaries")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "zcta_boundaries_missing"
    assert "ZIP boundary data is not loaded yet" in data["message"]


def test_api_zcta_boundaries_serves_optional_geojson(client, tmp_path, monkeypatch):
    path = tmp_path / "zcta_boundaries_simplified.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"GEOID20": "95112"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [-121.9, 37.3], [-121.8, 37.3], [-121.8, 37.4],
                [-121.9, 37.4], [-121.9, 37.3],
            ]]},
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "ZCTA_BOUNDARY_CANDIDATES", (path,))
    resp = client.get("/api/zcta-boundaries")
    assert resp.status_code == 200
    assert resp.json()["features"][0]["properties"]["GEOID20"] == "95112"


# --------------------------------------------------------------------------- #
# On-click reverse geocoding
# --------------------------------------------------------------------------- #
def test_api_reverse_geocode_returns_zip_and_address(client, monkeypatch):
    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "display_name": "123 Main St, Hayward, CA 94541, USA",
                "address": {"postcode": "94541"},
            }

    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return Resp()

    monkeypatch.setattr(web_app, "_REVERSE_GEOCODE_CACHE", {})
    monkeypatch.setattr(web_app.requests, "get", fake_get)
    data = client.get("/api/reverse-geocode?lat=37.6737&lng=-122.0878").json()
    assert data["zip"] == "94541"
    assert "Hayward" in data["address"]
    assert data["source"] == "OpenStreetMap Nominatim"
    assert calls[0][1]["format"] == "jsonv2"


def test_api_reverse_geocode_invalid_coordinates(client):
    resp = client.get("/api/reverse-geocode?lat=200&lng=-122")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_coordinates"


# --------------------------------------------------------------------------- #
# Dashboard HTML — new feature surfaces
# --------------------------------------------------------------------------- #
def test_root_has_comparison_and_validation(client):
    body = client.get("/").text
    assert "Add to comparison" in body            # Part 1
    assert "pane-compare" in body                 # comparison container
    assert "MAX_COMPARE" in body                  # max-size guard
    assert "Model validation" in body             # Part 4
    assert "/api/model-backtest" in body
    assert "Commercial validation" in body        # Part 2 (commercial card)
    assert "data-tab" in body                      # bottom-sheet tabs


def test_root_has_bulk_enrichment_surfaces(client):
    body = client.get("/").text
    # Bulk enrichment display fields present in the ZIP panel.
    for label in ("Healthcare providers", "Nursing schools", "Nearest hospital",
                  "bulk datasets"):
        assert label in body
    for label in ("API priority", "API candidate score",
                  "Recommended for live Places", "API filter"):
        assert label in body
    for text in ("Clicked location", "/api/reverse-geocode",
                 "Looking up address", "Address lookup unavailable"):
        assert text in body
    # Full-screen map (Apple Maps / Zillow feel).
    assert "100vh" in body
    # Bottom sheet can be dragged to reveal more or less map.
    assert "initSheetResizer" in body
    assert "pointermove" in body
    assert "--sheet-height" in body


def test_dashboard_visualization_modes_and_no_live_places_on_load(client, with_national):
    body = client.get("/").text
    for text in ("data-viz=\"points\"", "data-viz=\"heat\"", "data-viz=\"both\"",
                 # ZIP boundaries are an independent overlay toggle, not a view mode.
                 "data-boundary=\"off\"", "data-boundary=\"on\"", "boundary-seg",
                 "setBoundaries", "ZIP boundaries",
                 "data-heat-intensity=\"light\"", "data-heat-intensity=\"normal\"",
                 "data-heat-intensity=\"strong\"", "nearestHistorical",
                 "refreshModeledPointLayer", "Top ZIPs in current view",
                 "renderTopZipsInView", "plain_english_summary",
                 "recommended_next_action", "risk_flags",
                 "ensureZctaBoundaries", "renderBoundaries",
                 "normalizeModeledRow", "ensureModeledDetail"):
        assert text in body
    assert "GooglePlacesClient" not in body
    assert "GOOGLE_MAPS_API_KEY" not in body

    data = client.get("/api/national-demand").json()
    assert data["rows"][0]["zip"] == "95112"
    assert data["rows"][0]["overall_score"] == 74.9
    assert "population" not in data["rows"][0]
    # The dashboard endpoint annotates saved local JSON; it does not make live
    # Places calls on load or view-toggle.
    assert all("google_places_live_call" not in row for row in data["rows"])


def test_dashboard_has_modeled_vs_historical_explanation_notes(client):
    body = client.get("/").text
    for text in (
        "Market Opportunity Score v2.0",
        "Baseline v2.0 Market View",
        "Validation Confidence",
        "Historical Confidence",
        "Data Completeness",
        "2,694 priority ZIPs enriched",
        "Request manual validation",
        "Modeled score = public-data estimate.",
        "Historical score = real ALLCPR evidence.",
        "Smooth heat = regional intensity, not exact ZIP boundaries.",
        "ZIP detail panel = exact ZIP-level evidence.",
        "Google Places is context enrichment only and is not called on page load.",
        "Enriched validation available for priority ZIPs.",
        "No ALLCPR historical activity is attached to this ZIP. This is a public-data estimate only.",
        "This ZIP has real ALLCPR operational history.",
        "ZIP boundaries show exact ZCTA polygon shading where boundary data is available.",
        "ZIP boundary data is not available in this build. Use ZIP points or smooth heat.",
    ):
        assert text in body


def test_dashboard_has_site_priority_v21_section(client):
    body = client.get("/").text
    for text in (
        "Operator Decision",
        "Site Opening Decision",
        "Detailed score breakdown",
        "Final Site Priority Score",
        "Market Demand Score",
        "Validation Evidence Score",
        "Commercial Feasibility Score",
        "Competition Risk",
        "Decision",
        "Status",
        "Course fit",
        "Confidence",
        "What this means",
        "Why this fits the selected course",
        "Best next action",
        "Main blockers",
        "This is the baseline market-demand estimate. Use Site Priority v2.1 for opening decisions.",
        "Why this decision",
        "operatorDecisionHTML(row)",
        "courseOperatorProfile(row)",
        "statusBadge(statusText",
        "sitePriorityV21HTML(row)",
        "scoreBreakdownHTML(row)",
        "Why this final score",
        "Weighted subtotal",
        "Competition saturation penalty",
        "course_priority_profiles",
        "site_priority_score_breakdown",
        "final_site_priority_score",
        "score_formula_version",
    ):
        assert text in body
    assert "saturated_unless_differentiated" not in body
    assert "competitive_but_healthy" not in body


def test_dashboard_keeps_opportunity_score_separate_from_validation_confidence(client):
    body = client.get("/").text
    # Regression: a modeled ZIP with overall=61.9 must show 61.9 as
    # Market Opportunity Score v2.0, not as Validation Confidence.
    assert 'scoreCardHTML(t("marketOpportunityScoreV20"), `${fmt(row.overall)} / 100`' in body
    assert 'const validationDisplay = hasValidationEvidence && row.validation_score!=null ? `${fmt(row.validation_score)} / 100` : "—";' in body
    assert 'scoreCardHTML(t("validationConfidence"), validationDisplay' in body
    example = {"overall": 61.9, "validation_score": 83.9}
    assert f"{example['overall']:.1f} / 100" == "61.9 / 100"
    assert f"{example['validation_score']:.1f} / 100" != "61.9 / 100"


def test_dashboard_has_export_controls(client):
    body = client.get("/").text
    for text in (
        "Export top view CSV",
        "Export map view CSV",
        "Export top 100 CSV",
        "Export QA JSON",
        "Export ZIP JSON",
        "exportTopZipsCurrentViewCSV",
        "exportCurrentMapViewCSV",
        "exportSelectedZipDetailJSON",
        "exportNationalTopZipsCSV",
        "exportQASummaryJSON",
        "rowsToCSV",
        "downloadBlob",
    ):
        assert text in body


def test_dashboard_has_no_open_now_or_lease_ready(client):
    body = client.get("/").text.lower()
    for phrase in ("open now", "lease-ready", "ready to lease"):
        assert phrase not in body


def test_render_start_command_uses_one_worker_and_no_reload():
    text = (web_app.ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1" in text
    assert "--reload" not in text
    assert "--workers 2" not in text
    assert "--workers 3" not in text
