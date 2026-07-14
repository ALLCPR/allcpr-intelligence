"""Tests for offline API-candidate gating before live Places enrichment."""
from __future__ import annotations

import builtins
import json

from app.scoring.api_candidate_filter import (
    classify_api_candidate,
    compute_api_candidate_score,
    explain_api_filter,
    filter_api_candidates,
)
from scripts.select_api_candidates import (
    build_api_candidate_payload,
    filter_rows_by_radius,
    main,
)


def _row(**overrides):
    base = {
        "zip": "94541",
        "lat": 37.67,
        "lng": -122.08,
        "population": 67_401,
        "population_density": 9_040,
        "overall": 61.1,
        "data_confidence": "ok",
        "healthcare_employment_share": 0.01,
        "tier": "baseline",
    }
    base.update(overrides)
    return base


def test_hard_exclusion_for_missing_coordinates():
    row = _row(lat=None)
    assert compute_api_candidate_score(row) == 0.0
    assert classify_api_candidate(row) == "exclude"
    assert "Missing latitude/longitude" in explain_api_filter(row)


def test_hard_exclusion_for_low_population_density():
    row = _row(population=2_000, population_density=100)
    assert classify_api_candidate(row) == "exclude"
    reason = explain_api_filter(row)
    assert "Population below" in reason or "Population density below" in reason


def test_bulk_enriched_zip_without_meaningful_signal_is_excluded():
    row = _row(enrichment_sources=["HIFLD", "NPI"], hospital_count=0,
               healthcare_provider_count=0, college_count=0,
               community_facility_count=0, commercial_access_proxy_score=0)
    assert classify_api_candidate(row) == "exclude"
    assert "Bulk enrichment is present" in explain_api_filter(row)


def test_candidate_score_and_reason_for_dense_modeled_zip():
    row = _row()
    score = compute_api_candidate_score(row)
    assert score >= 40
    assert classify_api_candidate(row) in {"low", "medium", "high", "finalist"}
    reason = explain_api_filter(row)
    assert "API candidate" in reason
    assert "modeled demand" in reason


def test_filter_api_candidates_ranking_and_max_zips():
    weak = _row(zip="10000", overall=46, population_density=400)
    strong = _row(zip="20000", overall=80, hospital_count=4,
                  nursing_school_count=2, community_facility_count=8,
                  commercial_access_proxy_score=90)
    medium = _row(zip="30000", overall=65, healthcare_provider_count=20)
    selected = filter_api_candidates([weak, strong, medium], max_zips=2)
    assert [r["zip"] for r in selected] == ["20000", "30000"]
    assert all("api_candidate_score" in r for r in selected)


def test_filter_api_candidates_min_score():
    rows = [_row(zip="1", overall=55), _row(zip="2", overall=80,
            hospital_count=5, nursing_school_count=3,
            commercial_access_proxy_score=100)]
    selected = filter_api_candidates(rows, min_score=70)
    assert [r["zip"] for r in selected] == ["2"]


def test_select_payload_estimates_budget():
    rows = [_row(zip="1"), _row(zip="2", population=100)]
    payload = build_api_candidate_payload(rows, top=1)
    assert payload["total_zips"] == 2
    assert payload["excluded_zips"] == 1
    assert payload["selected_zips"] == 1
    assert payload["estimated_places_calls"] == 4
    assert payload["estimated_runtime_minutes"] >= 0
    assert payload["rows"][0]["reason"]


def test_radius_filter_limits_selector_scope():
    near = _row(zip="94541", lat=37.67, lng=-122.08)
    far = _row(zip="95112", lat=37.33, lng=-121.89)
    scoped = filter_rows_by_radius(
        [near, far],
        center_lat=37.6688,
        center_lng=-122.0808,
        radius_miles=5,
    )
    assert [r["zip"] for r in scoped] == ["94541"]
    assert scoped[0]["api_filter_distance_miles"] < 1


def test_select_payload_records_radius_selection():
    rows = [_row(zip="94541", lat=37.67, lng=-122.08),
            _row(zip="95112", lat=37.33, lng=-121.89)]
    payload = build_api_candidate_payload(
        rows,
        top=10,
        center_lat=37.6688,
        center_lng=-122.0808,
        radius_miles=5,
    )
    assert payload["scoped_zips"] == 1
    assert payload["selected_zips"] == 1
    assert payload["selection"]["radius_miles"] == 5
    assert payload["rows"][0]["distance_miles"] is not None


def test_select_script_never_imports_google_places(tmp_path, monkeypatch):
    source = tmp_path / "national.json"
    source.write_text(json.dumps({"rows": [_row()]}), encoding="utf-8")
    output = tmp_path / "api_candidates.json"

    real_import = builtins.__import__
    imports = []

    def tracking_import(name, *args, **kwargs):
        if name == "app.collectors.google_places":
            imports.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    assert main(["--input", str(source), "--output", str(output), "--top", "1"]) == 0
    assert imports == []
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["selected_zips"] == 1
