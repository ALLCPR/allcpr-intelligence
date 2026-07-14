"""Tests for the bulk public-data collectors (HIFLD / NPI / IPEDS / OSM)."""
from __future__ import annotations

import json

from app.collectors import hifld_facilities as hifld
from app.collectors import ipeds as ipeds_mod
from app.collectors import npi_bulk as npi
from app.collectors import osm_overpass_facilities as osm

CENTROIDS = {"95112": (37.33, -121.88), "97202": (45.48, -122.64)}


# --------------------------- HIFLD ---------------------------
def test_hifld_missing_file_is_empty(tmp_path):
    assert hifld.parse_facility_csv(tmp_path / "nope.csv", "hospital") == []
    assert hifld.load_hifld({"hospital": tmp_path / "nope.csv"}, CENTROIDS) == {}


def test_hifld_parse_and_aggregate_by_zip_column(tmp_path):
    p = tmp_path / "hosp.csv"
    p.write_text("NAME,LATITUDE,LONGITUDE,ZIP\n"
                 "A,37.33,-121.88,95112\nB,37.34,-121.89,95112\n"
                 "C,45.48,-122.64,97202\n", encoding="utf-8")
    out = hifld.load_hifld({"hospital": p}, CENTROIDS)
    assert out["95112"]["hospital_count"] == 2
    assert out["95112"]["healthcare_facility_count"] == 2
    assert "nearest_hospital_miles" in out["95112"]
    assert out["97202"]["hospital_count"] == 1


def test_hifld_assigns_by_centroid_when_no_zip(tmp_path):
    p = tmp_path / "ems.csv"
    p.write_text("NAME,LATITUDE,LONGITUDE\nStation,37.331,-121.881\n",
                 encoding="utf-8")
    out = hifld.load_hifld({"ems": p}, CENTROIDS)
    assert out["95112"]["ems_fire_count"] == 1


# --------------------------- NPI ---------------------------
def test_npi_classify_taxonomy():
    assert npi.classify_taxonomy("163W00000X") == "nurse"
    assert npi.classify_taxonomy("207Q00000X") == "physician"
    assert npi.classify_taxonomy("261QM0850X") == "other"
    assert npi.classify_taxonomy(None) == "other"


def test_npi_missing_file_is_empty(tmp_path):
    assert npi.aggregate_npi_by_zip(tmp_path / "nope.csv") == {}


def test_npi_streaming_aggregation(tmp_path):
    p = tmp_path / "npi.csv"
    p.write_text(
        "Provider Business Practice Location Address Postal Code,"
        "Healthcare Provider Taxonomy Code_1,Entity Type Code\n"
        "95112,163W00000X,1\n95112,207Q00000X,1\n95112,261QM0850X,2\n"
        "97202,163W00000X,1\n", encoding="utf-8")
    out = npi.aggregate_npi_by_zip(p)
    assert out["95112"]["healthcare_provider_count"] == 3
    assert out["95112"]["nurse_count"] == 1
    assert out["95112"]["physician_count"] == 1
    assert out["95112"]["clinic_provider_count"] == 1
    assert out["97202"]["nurse_count"] == 1


def test_npi_limit_caps_rows(tmp_path):
    p = tmp_path / "npi.csv"
    p.write_text(
        "Provider Business Practice Location Address Postal Code,"
        "Healthcare Provider Taxonomy Code_1,Entity Type Code\n"
        + "95112,163W00000X,1\n" * 10, encoding="utf-8")
    out = npi.aggregate_npi_by_zip(p, limit=3)
    assert out["95112"]["healthcare_provider_count"] == 3


# --------------------------- IPEDS ---------------------------
def test_ipeds_missing_file_is_empty(tmp_path):
    assert ipeds_mod.load_ipeds(tmp_path / "nope.csv") == {}


def test_ipeds_parse_and_classify(tmp_path):
    p = tmp_path / "ipeds.csv"
    p.write_text("INSTNM,ZIP,EFTOTLT\n"
                 "San Jose Nursing College,95112,1200\n"
                 "Downtown City College,95112,8000\n"
                 "Health Sciences Institute,97202,600\n", encoding="utf-8")
    out = ipeds_mod.load_ipeds(p)
    assert out["95112"]["college_count"] == 2
    assert out["95112"]["nursing_school_count"] == 1
    assert out["95112"]["health_program_school_count"] == 1
    assert out["95112"]["student_enrollment_count"] == 9200
    assert out["97202"]["health_program_school_count"] == 1


# --------------------------- OSM ---------------------------
def test_osm_missing_file_is_empty(tmp_path):
    assert osm.load_osm(tmp_path / "nope.json", CENTROIDS) == {}


def test_osm_parse_and_aggregate(tmp_path):
    p = tmp_path / "osm.json"
    p.write_text(json.dumps({"elements": [
        {"type": "node", "lat": 37.33, "lon": -121.88, "tags": {"amenity": "childcare"}},
        {"type": "node", "lat": 37.331, "lon": -121.881, "tags": {"amenity": "school"}},
        {"type": "node", "lat": 37.33, "lon": -121.88, "tags": {"amenity": "parking"}},
        {"type": "node", "lat": 99.0, "lon": 99.0, "tags": {"amenity": "childcare"}},  # dropped
    ]}), encoding="utf-8")
    out = osm.load_osm(p, CENTROIDS)
    assert out["95112"]["childcare_count"] == 1
    assert out["95112"]["school_count"] == 1
    assert out["95112"]["community_facility_count"] == 2
    assert out["95112"]["parking_proxy_score"] > 0
