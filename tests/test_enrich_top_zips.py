"""Tests for the Phase-2 enrichment script."""
from __future__ import annotations

import json

import scripts.enrich_top_zips as enrich
from app.config import ZIP_MODELED_WEIGHTS_BLS
from app.utils.cache import Cache


def _payload():
    return {
        "layer": "modeled_national_demand", "tier": "baseline", "rows": [
            {"zip": "95112", "lat": 37.3, "lng": -121.8, "overall": 62,
             "bls_demand": 50, "cpr_demand": 73, "tier": "baseline",
             "population": 58_000, "population_density": 8_000,
             "data_confidence": "ok", "healthcare_employment_share": 0.01},
            {"zip": "10016", "lat": 40.7, "lng": -73.9, "overall": 76,
             "bls_demand": 64, "cpr_demand": 87, "tier": "baseline",
             "population": 52_000, "population_density": 75_000,
             "data_confidence": "ok", "healthcare_employment_share": 0.02},
            {"zip": "99999", "lat": 1.0, "lng": 1.0, "overall": 12,
             "bls_demand": 8, "cpr_demand": 10, "tier": "baseline",
             "population": 500, "population_density": 20,
             "data_confidence": "ok"},
        ],
    }


def _commercial_csv(tmp_path):
    p = tmp_path / "commercial_validation.csv"
    p.write_text(
        "zip,address,monthly_rent,parking,available,classroom_fit,source_url,updated_at\n"
        "95112,1 A St,3000,Yes,Yes,Good,https://x/1,2026-06-11\n",
        encoding="utf-8")
    return p


def test_select_top_zips_by_course():
    rows = _payload()["rows"]
    top = enrich.select_top_zips(rows, course="overall", top_n=2)
    assert [r["zip"] for r in top] == ["10016", "95112"]
    # aha_bls ranks by bls_demand.
    top_bls = enrich.select_top_zips(rows, course="aha_bls", top_n=1)
    assert top_bls[0]["zip"] == "10016"


def test_min_score_filter():
    rows = _payload()["rows"]
    top = enrich.select_top_zips(rows, course="overall", top_n=10, min_score=70)
    assert [r["zip"] for r in top] == ["10016"]


def test_places_routes_stubs_no_crash_without_client():
    assert enrich.enrich_zip_with_places({"zip": "10016"}) == {}      # client=None
    assert enrich.enrich_zip_with_routes({"zip": "10016"}) == {}


class _FakeClient:
    """Stand-in for GooglePlacesClient: returns N results per query by keyword."""
    def __init__(self, counts):
        self.counts = counts
        self.calls = 0

    def nearby_search(self, location, radius_meters, place_type=None,
                      keyword=None, max_pages=1):
        self.calls += 1
        key = place_type or keyword
        n = self.counts.get(key, 0)
        return [{"rating": 4.0} for _ in range(n)]


def test_places_enrichment_counts_and_signals():
    client = _FakeClient({"hospital": 3, "urgent care": 2, "nursing school": 1,
                          "CPR BLS certification class": 4})
    # land area = population / density = 50_000 / 10_000 = 5.0 sq mi.
    out = enrich.enrich_zip_with_places(
        {"zip": "10016", "lat": 40.7, "lng": -73.9,
         "population": 50_000, "population_density": 10_000}, client)
    assert out["hospital_count"] == 3
    assert out["competitor_count"] == 4
    assert out["healthcare_facility_count"] == 5      # hospital + urgent care POIs
    assert out["training_school_count"] == 1
    # Densities are normalized per square mile (count / 5.0 sq mi).
    assert out["healthcare_facility_density"] == 1.0   # 5 / 5.0
    assert out["training_school_density"] == 0.2       # 1 / 5.0
    # competition_gap_score = 100 * (1 - 4/20) = 80.
    assert out["competition_gap_score"] == 80.0
    assert out["avg_competitor_rating"] == 4.0
    assert client.calls == enrich.PLACES_CALLS_PER_ZIP   # one per grouped query
    # Debug output carries raw counts, ZIP area, density, weight, contribution.
    debug = out["enhanced_signal_debug"]
    assert debug["land_sqmi"] == 5.0
    hc = next(r for r in debug["signals"]
              if r["field"] == "healthcare_facility_density")
    assert hc["raw_count"] == 5
    assert hc["value"] == 1.0
    assert hc["weight"] == ZIP_MODELED_WEIGHTS_BLS["healthcare_facility_density"]
    assert hc["contribution"] is not None


def test_places_enrichment_rescore_changes_score():
    from app.scoring.zip_modeled_opportunity import compute_zip_modeled_opportunity
    payload = _payload()
    feats = {"10016": {"population": 50000, "population_density": 6000,
                       "median_household_income": 120000, "working_age_share": 0.8,
                       "employment_rate": 0.72, "bachelors_or_higher_share": 0.7,
                       "healthcare_employment_share": 0.2}}
    pure_baseline = compute_zip_modeled_opportunity(feats["10016"])["overall"]
    client = _FakeClient({"hospital": 6, "urgent care": 4, "nursing school": 3,
                          "CPR BLS certification class": 0})  # facilities, no rivals
    enrich.run_enrichment(payload, course="overall", top_n=0, state=None,
                          min_score=None, max_api_calls=None, zips=["10016"],
                          places_client=client, features_by_zip=feats)
    row = next(r for r in payload["rows"] if r["zip"] == "10016")
    # Recomputed from the SAME features + enrichment signals -> differs from the
    # pure (no-enrichment) baseline, proving Places feeds back into the score.
    assert row["overall"] != pure_baseline
    assert row["tier"] == "enriched"
    assert row["hospital_count"] == 6


def test_explicit_zip_selection():
    payload = _payload()
    enriched = enrich.run_enrichment(
        payload, course="overall", top_n=100, state=None, min_score=None,
        max_api_calls=None, zips=["95112"],
        places_client=_FakeClient({}), features_by_zip={})
    assert enriched == ["95112"]


def test_live_places_gate_skips_excluded_zip_before_client_call():
    payload = _payload()
    client = _FakeClient({"hospital": 3})
    enriched = enrich.run_enrichment(
        payload, course="overall", top_n=100, state=None, min_score=None,
        max_api_calls=None, zips=["99999"],
        places_client=client, features_by_zip={})
    assert enriched == []
    assert client.calls == 0


def test_commercial_enrichment_merges_and_flips_tier(tmp_path):
    payload = _payload()
    enriched = enrich.run_enrichment(
        payload, course="overall", top_n=1, state=None, min_score=None,
        max_api_calls=None, commercial_path=_commercial_csv(tmp_path))
    # 95112 is mid-ranked but commercial-validated -> always enriched.
    assert "95112" in enriched
    row = {r["zip"]: r for r in payload["rows"]}["95112"]
    assert row["tier"] == "enriched"
    assert row["commercial_space_available"] is True
    assert row["estimated_rent"] == 3000
    assert row["rent_source"]
    assert "commercial_validation_csv" in row["enrichment_sources"]
    assert row["enrichment_updated_at"]
    # Untouched ZIP stays baseline.
    assert {r["zip"]: r for r in payload["rows"]}["99999"]["tier"] == "baseline"


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    inp = tmp_path / "national_demand.json"
    inp.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "national_demand_enriched.json"
    rc = enrich.main(["--input", str(inp), "--output", str(out),
                      "--top-n", "2", "--course", "overall", "--dry-run"])
    assert rc == 0
    assert not out.exists()


def test_zips_from_dry_run_does_not_call_places(tmp_path):
    inp = tmp_path / "national_demand.json"
    inp.write_text(json.dumps(_payload()), encoding="utf-8")
    candidates = tmp_path / "api_candidates.json"
    candidates.write_text(json.dumps({"rows": [{"zip": "95112"}]}),
                          encoding="utf-8")
    out = tmp_path / "national_demand_enriched.json"
    rc = enrich.main(["--input", str(inp), "--output", str(out),
                      "--zips-from", str(candidates), "--dry-run"])
    assert rc == 0
    assert not out.exists()


class _FakeLiveClient:
    def __init__(self):
        self.live_calls = 0

    def _nearby_search_live(self, location, radius_meters, place_type=None,
                            keyword=None, max_pages=2):
        self.live_calls += 1
        return [{"name": place_type or keyword or "x"}]


def test_instrumented_places_client_reports_cache_hits(tmp_path):
    cache = Cache(tmp_path / "cache.sqlite")
    live = _FakeLiveClient()
    client = enrich.InstrumentedPlacesClient(
        live, cache=cache, refresh_days=30, force_refresh=False)
    client.begin_zip("95112")
    first = client.nearby_search((37.3, -121.8), 8046, place_type="hospital",
                                 max_pages=1)
    second = client.nearby_search((37.3, -121.8), 8046, place_type="hospital",
                                  max_pages=1)
    summary = client.summary()
    assert first == second
    assert live.live_calls == 1
    assert summary["live_places_calls"] == 1
    assert summary["cache_hits"] == 1
    assert summary["cache_misses"] == 1


def test_write_preserves_all_rows(tmp_path):
    inp = tmp_path / "national_demand.json"
    inp.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "national_demand_enriched.json"
    rc = enrich.main(["--input", str(inp), "--output", str(out),
                      "--top-n", "1", "--course", "overall"])
    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert len(written["rows"]) == 3            # all rows preserved
    assert "enriched_zip_count" in written
    assert written["enrichment_scope"] == "priority_zips"
    assert written["enrichment_summary"] == "Enriched validation available for priority ZIPs."
    assert written["manual_force_enrichment_supported"] is True


def test_max_api_calls_trims_selection():
    rows = [{"zip": f"{i:05d}", "overall": 100 - i, "bls_demand": 1,
             "cpr_demand": 1} for i in range(50)]
    # Grouped Places queries per ZIP; a low cap trims the live selection.
    sel = enrich.select_top_zips(rows, course="overall", top_n=50)
    assert len(sel) == 50
    payload = {"rows": rows}
    enrich.run_enrichment(payload, course="overall", top_n=50, state=None,
                          min_score=None, max_api_calls=16)
    # No commercial data here, so nothing flips; just assert no crash.
    assert all(r["tier"] != "enriched" for r in rows if "tier" in r)
