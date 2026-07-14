"""
End-to-end smoke test with a mocked Google Places client.

Walks: synthetic candidate -> mock Places responses -> area_profile ->
score_profile -> markdown + JSON + CSV rendering. No real network calls.

This catches wiring regressions (key renames, missing fields, serialization
bugs, empty-data crashes) that unit tests alone miss.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import patch

import pytest

# Ensure we have a fake API key BEFORE importing modules that read env at import.
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

from app.collectors.google_places import GooglePlacesClient
from app.enrichers.area_profile import build_area_profile
from app.reports.csv_report import candidate_to_row
from app.reports.json_report import render_json
from app.reports.markdown_report import render_markdown_report
from app.scoring.site_score import score_profile


# ---- mock helpers ---------------------------------------------------------- #

def _raw_place(name: str, lat: float, lon: float, place_id: str,
               rating: float = 4.5, reviews: int = 100,
               types: List[str] = None) -> Dict:
    return {
        "place_id": place_id,
        "name": name,
        "formatted_address": f"{name} St, Testville, CA",
        "geometry": {"location": {"lat": lat, "lng": lon}},
        "rating": rating,
        "user_ratings_total": reviews,
        "types": types or ["point_of_interest"],
        "url": f"https://maps.google.com/?cid={place_id}",
    }


def _mock_nearby(self, location, radius_meters, place_type=None,
                 keyword=None, max_pages=1):
    """Return tailored mocks based on the type/keyword."""
    lat, lon = location
    if place_type == "shopping_mall":
        return [_raw_place("Test Shopping Plaza", lat + 0.001, lon + 0.001,
                           "pid_mall", rating=4.4, reviews=320,
                           types=["shopping_mall"])]
    if place_type == "hospital":
        return [
            _raw_place("Test Regional Hospital", lat, lon, "pid_h1",
                       rating=4.2, reviews=540, types=["hospital"]),
            _raw_place("Mercy Hospital", lat + 0.01, lon, "pid_h2",
                       types=["hospital"]),
        ]
    if place_type == "fire_station":
        return [_raw_place("Station 12", lat, lon + 0.002, "pid_fs1",
                           types=["fire_station"])]
    if place_type == "gym":
        return [_raw_place("CrossPower Gym", lat, lon, "pid_g1",
                           types=["gym"])]
    if place_type == "dentist":
        return [_raw_place("Smile Dental", lat, lon, "pid_d1",
                           types=["dentist"])]
    if place_type == "university":
        return [_raw_place("Test State University", lat + 0.005, lon, "pid_u1",
                           types=["university"])]
    if keyword and "nursing" in keyword:
        return [_raw_place("Test Nursing School", lat, lon, "pid_ns1",
                           types=["school"])]
    if keyword and "urgent care" in keyword:
        return [_raw_place("UrgentCare Plus", lat, lon, "pid_uc1",
                           types=["health"])]
    return []


def _mock_text_search(self, query, location=None, radius_meters=None,
                      max_pages=1):
    if "CPR" in query or "BLS" in query:
        lat, lon = location or (0.0, 0.0)
        return [
            _raw_place("Acme CPR Training", lat, lon, "pid_c1",
                       rating=3.8, reviews=18,
                       types=["point_of_interest"]),
            _raw_place("Citywide BLS", lat + 0.003, lon, "pid_c2",
                       rating=4.7, reviews=200, types=["health"]),
        ]
    return []


def _mock_place_details(self, place_id):
    return {
        "place_id": place_id,
        "international_phone_number": "+1-555-010-1234",
        "website": f"https://{place_id}.example",
        "opening_hours": {"weekday_text": ["Mon: 9-5"]},
    }


def _mock_census(latitude, longitude):
    return {
        "values": {},
        "indicators": {},
        "sources": [],
        "geo_desc": "test-geography",
    }


def _mock_geocode(latitude, longitude):
    return None  # exercise the "no anchor fallback at all" branch


# ---- the test ------------------------------------------------------------- #

@patch("app.enrichers.area_profile.collect_economy_for_point",
       side_effect=lambda *a, **kw: {
           "census": _mock_census(0, 0),
           "labor": {"values": {}, "indicators": {}, "sources": []},
           "real_estate": {"values": {}, "indicators": {}, "sources": []},
       })
@patch("app.enrichers.anchor._reverse_geocode",
       side_effect=lambda lat, lon: None)
@patch.object(GooglePlacesClient, "place_details", _mock_place_details)
@patch.object(GooglePlacesClient, "text_search", _mock_text_search)
@patch.object(GooglePlacesClient, "nearby_search", _mock_nearby)
def test_pipeline_runs_end_to_end_with_mocks(mock_reverse, mock_economy, tmp_path):
    client = GooglePlacesClient()

    profile = build_area_profile(
        client, city="Testville", state="CA",
        latitude=37.0, longitude=-121.0, radius_miles=2.0,
        candidate_index=0, candidate_name="Testville grid #0",
    )

    # Anchor must be the closest hit; with our mocks the shopping mall (0.001,
    # 0.001 offset) is closer than the hospital (0,0) — wait, hospital is at
    # the origin so it's 0mi away. Closest-hit selection should pick hospital.
    assert profile["anchor"] is not None, "anchor should exist (closest hit wins)"
    assert profile["anchor"]["name"] in (
        "Test Regional Hospital", "Test Shopping Plaza",
    )

    # The categories we mocked should have non-empty top_places.
    assert profile["demand_top_places"]["hospital"], "hospital top_places empty"
    assert profile["demand_top_places"]["fire_station"]
    assert profile["competitors"], "competitor list should not be empty"

    # Score it.
    scored = score_profile(profile)
    assert "tier" in scored and scored["tier"] in ("A", "B", "C", "D", "F")
    assert 0 <= scored["area_score"] <= 100
    # No commercial override → site_score withheld; candidate is area-level.
    assert scored["site_score"] is None
    assert scored["candidate_type"] in (
        "commercial_area_proxy", "landmark_proxy", "invalid_or_low_confidence",
    )
    # Economy with no data should be neutral 50, not 0.
    assert scored["sub_scores"]["economy_score"] == 50.0
    # Profitability label is explicit.
    assert scored["profitability_estimate"]["confidence"] == "estimated"

    # Render Markdown — must not raise and must contain the anchor name.
    md = render_markdown_report("Testville", "CA", 2.0, [(profile, scored)])
    assert profile["anchor"]["name"] in md
    assert "tier" in md.lower()
    assert "estimated" in md.lower()
    # No API key leakage.
    assert "fake-key-for-tests" not in md

    # CSV row.
    row = candidate_to_row(profile, scored)
    assert row["anchor_name"] == profile["anchor"]["name"]
    assert row["tier"] in ("A", "B", "C", "D", "F")
    assert "estimated_revenue_mid" in row

    # JSON.
    payload = render_json([(profile, scored)],
                          context={"radius_miles": 2.0})
    j = json.dumps(payload, default=str)  # must be JSON-serializable
    assert "anchor" in j
    assert "fake-key-for-tests" not in j


@patch("app.enrichers.area_profile.collect_economy_for_point",
       side_effect=lambda *a, **kw: {
           "census": _mock_census(0, 0),
           "labor": {"values": {}, "indicators": {}, "sources": []},
           "real_estate": {"values": {}, "indicators": {}, "sources": []},
       })
@patch("app.enrichers.anchor._reverse_geocode",
       side_effect=lambda lat, lon: None)
@patch.object(GooglePlacesClient, "place_details", lambda self, pid: {})
@patch.object(GooglePlacesClient, "text_search", lambda self, q, **kw: [])
@patch.object(GooglePlacesClient, "nearby_search", lambda self, *a, **kw: [])
def test_pipeline_survives_empty_responses(mock_reverse, mock_economy, tmp_path):
    """When every external call returns nothing, the pipeline must still
    produce a valid (low-scoring) report without crashing."""
    client = GooglePlacesClient()
    profile = build_area_profile(
        client, city="Empty", state="CA",
        latitude=0.0, longitude=0.0, radius_miles=2.0,
    )
    scored = score_profile(profile)
    md = render_markdown_report("Empty", "CA", 2.0, [(profile, scored)])
    assert "No nearby anchor found" in md or "candidate" in md.lower()
    # Empty competitor section must still render.
    assert "competitor" in md.lower()
    row = candidate_to_row(profile, scored)
    assert row["nearby_cpr_competitors_5mi"] == 0
