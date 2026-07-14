"""Tests for the national modeled-demand build (no network)."""
from __future__ import annotations

import json

from scripts.build_national_demand import (
    ENRICHMENT_DESCRIPTIVE_FIELDS,
    ENRICHMENT_META_FIELDS,
    build_national_payload,
)
from scripts.build_zip_centroids import parse_gazetteer_records

GAZ = {
    "95112": {"lat": 37.33, "lng": -121.88, "land_sqmi": 5.0},
    "10016": {"lat": 40.74, "lng": -73.97, "land_sqmi": 0.4},
    "00000": {"lat": 1.0, "lng": 1.0, "land_sqmi": 0.0},   # no land area
}
ACS = {
    "95112": {"population": 30_000, "median_household_income": 95_000,
              "working_age_share": 0.78, "employment_rate": 0.70,
              "bachelors_or_higher_share": 0.50, "healthcare_employment_share": 0.22},
    "10016": {"population": 52_000, "median_household_income": 140_000,
              "working_age_share": 0.86, "employment_rate": 0.72,
              "bachelors_or_higher_share": 0.80, "healthcare_employment_share": 0.18},
    "00000": {"population": None},   # no usable signal → omitted
}


def test_payload_shape_and_labeling():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022)
    assert p["layer"] == "modeled_national_demand"
    assert p["tier"] == "baseline"
    assert p["acs_vintage"] == 2022
    assert "methodology" in p and "estimate" in p["methodology"].lower()
    assert p["zip_count"] == len(p["rows"])
    # JSON-serializable.
    json.dumps(p)


def test_rows_have_required_fields_and_density():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022)
    by_zip = {r["zip"]: r for r in p["rows"]}
    assert "95112" in by_zip and "10016" in by_zip
    r = by_zip["95112"]
    for f in ("zip", "lat", "lng", "overall", "bls_demand", "cpr_demand",
              "tier", "recommendation", "population_density", "data_confidence"):
        assert f in r
    # density = population / land area = 30000 / 5
    assert r["population_density"] == 6000
    assert 0 <= r["overall"] <= 100
    assert r["tier"] == "baseline"


def test_zip_without_usable_signal_is_omitted_not_zeroed():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022)
    assert "00000" not in {r["zip"] for r in p["rows"]}


def test_rows_sorted_by_overall_desc():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022)
    overalls = [r["overall"] for r in p["rows"]]
    assert overalls == sorted(overalls, reverse=True)


def test_limit_caps_rows():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022, limit=1)
    assert p["zip_count"] == 1


def test_missing_land_area_drops_density_signal_safely():
    # A ZIP with land_sqmi 0 but real ACS still scores (density just omitted).
    gaz = {"22222": {"lat": 5.0, "lng": 5.0, "land_sqmi": 0.0}}
    acs = {"22222": {"population": 25_000, "median_household_income": 80_000,
                     "working_age_share": 0.75, "employment_rate": 0.68,
                     "bachelors_or_higher_share": 0.40,
                     "healthcare_employment_share": 0.15}}
    p = build_national_payload(gaz, acs, acs_vintage=2022)
    assert p["zip_count"] == 1
    assert p["rows"][0]["population_density"] is None
    assert p["rows"][0]["overall"] is not None


# ----- Phase-2 enrichment forward-compat -----
def test_baseline_rows_omit_enrichment_fields():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022)
    for row in p["rows"]:
        for f in (*ENRICHMENT_DESCRIPTIVE_FIELDS, *ENRICHMENT_META_FIELDS):
            assert f not in row   # lean baseline; fields appear only when enriched


def test_enrichment_attaches_fields_and_updates_score():
    baseline = {r["zip"]: r for r in
                build_national_payload(GAZ, ACS, acs_vintage=2022)["rows"]}["95112"]
    enrichment = {
        "95112": {
            "hospital_count": 3, "urgent_care_count": 5, "competitor_count": 2,
            "estimated_rent": 4200, "rent_source": "broker",
            "enrichment_tier": "places", "enrichment_sources": ["Google Places"],
            "enrichment_updated_at": "2026-06-11",
            # signal-bearing fields fold into the score:
            "healthcare_facility_density": 14, "competition_gap_score": 90,
        }
    }
    p = build_national_payload(GAZ, ACS, acs_vintage=2022,
                               enrichment_by_zip=enrichment)
    row = {r["zip"]: r for r in p["rows"]}["95112"]
    # Descriptive + meta fields attached.
    assert row["hospital_count"] == 3
    assert row["estimated_rent"] == 4200
    assert row["rent_source"] == "broker"
    assert row["enrichment_tier"] == "places"
    assert row["enrichment_sources"] == ["Google Places"]
    assert row["enrichment_updated_at"] == "2026-06-11"
    # Tier flips and the score reflects the new signals (changed vs baseline).
    assert row["tier"] == "enriched"
    assert row["overall"] != baseline["overall"]


def test_enrichment_for_other_zip_does_not_touch_baseline_zip():
    p = build_national_payload(GAZ, ACS, acs_vintage=2022,
                               enrichment_by_zip={"10016": {"hospital_count": 9,
                                                            "enrichment_tier": "places"}})
    by_zip = {r["zip"]: r for r in p["rows"]}
    assert by_zip["10016"]["hospital_count"] == 9
    assert "hospital_count" not in by_zip["95112"]
    assert by_zip["95112"]["tier"] == "baseline"


def test_rural_low_density_zip_is_capped_unless_validated():
    gaz = {"98625": {"lat": 45.94, "lng": -122.72, "land_sqmi": 195.1}}
    acs = {"98625": {
        "population": 6_827,
        "median_household_income": 92_000,
        "working_age_share": 0.70,
        "employment_rate": 0.66,
        "bachelors_or_higher_share": 0.34,
        "healthcare_employment_share": 0.006,
    }}
    row = build_national_payload(gaz, acs, acs_vintage=2024)["rows"][0]
    assert row["population_density"] == 35
    assert row["overall"] <= 25
    assert row["bls_demand"] <= 25
    assert row["recommendation"] == "Low priority"
    assert row["final_cap_applied"] is True
    assert row["cap_reason"] in {
        "rural_low_density_cap",
        "small_low_density_market_cap",
        "rural_weak_healthcare_workforce_cap",
    }
    assert "Higher income alone is not enough" in row["plain_english_summary"]
    assert any("final cap applied" in w for w in row["score_weaknesses"])


# ----- gazetteer records parser -----
def test_parse_gazetteer_records_reads_land_area():
    text = (
        "USPS\tGEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
        "95112\t95112\t12950000\t0\t5.0\t0.0\t37.33\t-121.88\n"
        "BAD\tABCDE\t0\t0\t0\t0\tx\ty\n"          # bad GEOID + coords → skipped
    )
    recs = parse_gazetteer_records(text)
    assert "95112" in recs and "ABCDE" not in recs
    assert recs["95112"]["land_sqmi"] == 5.0
    assert recs["95112"]["lat"] == 37.33
