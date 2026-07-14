"""Tests for the bulk-enrichment build + display-only merge."""
from __future__ import annotations

from scripts.build_bulk_enrichment import (
    build_bulk_payload,
    merge_bulk_into_national,
)

NATIONAL_ROWS = [
    {"zip": "95112", "lat": 37.33, "lng": -121.88, "overall": 61.9,
     "bls_demand": 50.5, "cpr_demand": 73.3, "tier": "baseline",
     "population": 55000},
    {"zip": "99999", "lat": 1.0, "lng": 1.0, "overall": 12.0,
     "bls_demand": 8.0, "cpr_demand": 10.0, "tier": "baseline",
     "population": 1000},
]


def _sources(tmp_path):
    hosp = tmp_path / "hosp.csv"
    hosp.write_text("NAME,LATITUDE,LONGITUDE,ZIP\nA,37.33,-121.88,95112\n",
                    encoding="utf-8")
    npi = tmp_path / "npi.csv"
    npi.write_text(
        "Provider Business Practice Location Address Postal Code,"
        "Healthcare Provider Taxonomy Code_1,Entity Type Code\n"
        "95112,163W00000X,1\n95112,207Q00000X,1\n", encoding="utf-8")
    ipeds = tmp_path / "ipeds.csv"
    ipeds.write_text("INSTNM,ZIP,EFTOTLT\nNursing College,95112,1200\n",
                     encoding="utf-8")
    return {"hospital": hosp}, npi, ipeds, None


def test_build_bulk_payload_combines_sources(tmp_path):
    hifld, npi, ipeds, osm = _sources(tmp_path)
    payload = build_bulk_payload(NATIONAL_ROWS, hifld_sources=hifld,
                                 npi_path=npi, ipeds_path=ipeds, osm_path=osm)
    assert set(payload["sources"]) == {"HIFLD", "NPI", "IPEDS"}
    row = {r["zip"]: r for r in payload["rows"]}["95112"]
    assert row["hospital_count"] == 1
    assert row["healthcare_provider_count"] == 2
    assert row["nursing_school_count"] == 1
    assert row["enrichment_tier"] == "bulk_enriched"
    # provider density per 10k = 2 / 55000 * 10000
    assert row["provider_density_per_10k_pop"] == round(2 / 55000 * 10000, 1)


def test_missing_all_sources_does_not_crash():
    payload = build_bulk_payload(NATIONAL_ROWS, hifld_sources={},
                                 npi_path=None, ipeds_path=None, osm_path=None)
    assert payload["zip_count"] == 0
    assert payload["sources"] == []


def test_merge_is_display_only_score_unchanged(tmp_path):
    hifld, npi, ipeds, osm = _sources(tmp_path)
    bulk = build_bulk_payload(NATIONAL_ROWS, hifld_sources=hifld, npi_path=npi,
                              ipeds_path=ipeds, osm_path=osm)
    national = {"rows": [dict(r) for r in NATIONAL_ROWS]}
    merged = merge_bulk_into_national(national, bulk["rows"])
    by_zip = {r["zip"]: r for r in merged["rows"]}
    enriched = by_zip["95112"]
    # Score is NOT changed by enrichment (scoring gate).
    assert enriched["overall"] == 61.9
    assert enriched["bls_demand"] == 50.5
    assert enriched["tier"] == "enriched"
    assert enriched["hospital_count"] == 1
    # Untouched ZIP stays baseline with no bulk fields.
    assert by_zip["99999"]["tier"] == "baseline"
    assert "hospital_count" not in by_zip["99999"]
    assert merged["enriched_zip_count"] == 1


def test_merge_preserves_commercial(tmp_path):
    hifld, npi, ipeds, osm = _sources(tmp_path)
    bulk = build_bulk_payload(NATIONAL_ROWS, hifld_sources=hifld, npi_path=npi,
                              ipeds_path=ipeds, osm_path=osm)
    national = {"rows": [dict(r) for r in NATIONAL_ROWS]}
    summaries = {"95112": {"commercial_validated": True, "commercial_ready": True}}
    merged = merge_bulk_into_national(national, bulk["rows"], summaries)
    row = {r["zip"]: r for r in merged["rows"]}["95112"]
    assert row["commercial"]["commercial_validated"] is True
    assert row["hospital_count"] == 1   # bulk + commercial coexist
