"""Phase 2 feature tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

from app.collectors import job_postings, real_estate
from app.collectors.website_analysis import analyze_website
from app.reports.csv_report import candidate_to_row
from app.reports.html_report import render_html_report
from app.reports.markdown_report import render_markdown_report
from app.scoring.economy_score import compute_accessibility_score
from app.scoring.job_demand_score import compute_job_demand_score
from app.scoring.rent_score import compute_rent_score
from app.scoring.site_score import score_profile
from app.utils.candidate_dedup import deduplicate_ranked_candidates
from app.utils.geo_utils import LatLon
from app.utils.report_safety import strip_sensitive_query_params
from app.utils.source_tracker import utcnow_iso
from scripts import full_pipeline


def _profile(candidate_id: str = "P1", city: str = "Testville",
             lat: float = 37.0, lon: float = -121.0) -> Dict:
    return {
        "candidate_id": candidate_id,
        "city": city,
        "state": "CA",
        "latitude": lat,
        "longitude": lon,
        "candidate_name": f"{city} candidate",
        "mode": "metro_comparison",
        "comparison_area": city,
        "radius_miles": 2.0,
        "anchor": {
            "place_id": f"anchor-{candidate_id}",
            "name": f"{city} Plaza",
            "formatted_address": f"1 {city} Way, CA",
            "latitude": lat,
            "longitude": lon,
            "google_maps_url": f"https://maps.example/?key=SECRET&place={candidate_id}",
            "website": "https://anchor.example/?token=SECRET",
            "photos": [{
                "photo_reference": "photo-ref",
                "width": 800,
                "height": 600,
                "attributions": ["Test attribution"],
            }],
        },
        "counts_5mi": {"hospital": 2, "fire_station": 1, "nursing_school": 1},
        "counts_by_bucket": {
            "hospital": {1: 1, 3: 2, 5: 2, 10: 2},
            "fire_station": {1: 1, 3: 1, 5: 1, 10: 1},
            "nursing_school": {1: 0, 3: 1, 5: 1, 10: 1},
        },
        "top_demand_drivers": [("hospital", 2), ("fire_station", 1)],
        "demand_top_places": {},
        "competition_summary": {
            "competitor_count_total": 1,
            "competitor_count_by_bucket_mi": {1: 0, 3: 1, 5: 1, 10: 1},
            "competitor_avg_rating": 4.1,
            "competitor_total_reviews": 10,
            "competitor_no_website": 0,
            "competitor_no_phone": 0,
            "competitor_low_rating_count": 0,
            "website_analysis_checked_count": 1,
            "competitor_online_booking_missing": 1,
            "competitor_class_schedule_missing": 1,
            "competitor_pricing_missing": 1,
        },
        "competitors": [{
            "name": "Acme CPR",
            "formatted_address": "2 Main St",
            "distance_miles": 1.1,
            "rating": 4.1,
            "user_ratings_total": 10,
            "website": "https://competitor.example",
            "website_analysis": {
                "checked": True,
                "detected": ["certification_keywords"],
                "missing": ["online_booking", "class_schedule", "pricing"],
                "unknown": [],
            },
        }],
        "competitors_sample": [],
        "accessibility": {
            "signals": {
                "freeway_major_road_proximity": {
                    "status": "detected", "distance_miles": 0.4,
                    "nearest_name": "US 101", "notes": "proxy",
                },
                "transit_station_proximity": {
                    "status": "detected", "distance_miles": 0.8,
                    "nearest_name": "Transit", "notes": "proxy",
                },
                "airport_business_corridor_proximity": {
                    "status": "detected", "distance_miles": 3.0,
                    "nearest_name": "Business Park", "notes": "proxy",
                },
                "shopping_center_plaza_proximity": {
                    "status": "detected", "distance_miles": 0.2,
                    "nearest_name": "Retail Plaza", "notes": "proxy",
                },
                "parking_proxy": {
                    "status": "detected", "distance_miles": 0.2,
                    "nearest_name": "Retail Plaza",
                    "notes": "Exact parking is unknown; proxy only.",
                },
                "walkability_proxy": {
                    "status": "detected", "nearby_places_1mi": 10,
                    "notes": "proxy",
                },
            },
        },
        "economy": {
            "census": {
                "values": {
                    "population": 80_000,
                    "median_household_income": 85_000,
                    "median_age": 37,
                },
                "indicators": {
                    "healthcare_employment_share": 0.13,
                    "working_age_share": 0.68,
                    "bachelors_or_higher_share": 0.33,
                    "employment_rate": 0.64,
                },
                "sources": [],
                "geo_desc": "test place",
            },
            "labor": {"values": {}, "indicators": {}, "sources": []},
            "real_estate": {
                "values": {
                    "rent_per_sqft_annual": None,
                    "rent_data_confidence": "unknown",
                    "rent_source": "",
                    "rent_notes": "No rent override matched; rent is unknown.",
                },
                "indicators": {},
                "sources": [],
            },
        },
        "job_demand": {
            "values": {
                "active_postings_count": None,
                "certification_postings_count": None,
                "bls_count": None,
                "cpr_count": None,
                "first_aid_count": None,
                "acls_count": None,
                "pals_count": None,
                "aha_red_cross_count": None,
                "healthcare_role_count": None,
                "emt_role_count": None,
                "cna_role_count": None,
                "caregiver_role_count": None,
                "dental_role_count": None,
                "childcare_role_count": None,
                "unique_employers_count": None,
            },
            "top_employers": [],
            "sample_postings": [],
            "sources": [{
                "name": "Public job postings CSV (not provided)",
                "url": "",
                "fields": [],
                "collected_at": utcnow_iso(),
                "notes": "unknown",
            }],
        },
        "sources": [{
            "name": "Google Places API (Nearby Search)",
            "url": "https://maps.googleapis.com/maps/api/place/nearbysearch/json?key=SECRET",
            "fields": ["candidate_anchor", "nearby_hospital"],
            "collected_at": utcnow_iso(),
        }],
        "source_urls": ["https://maps.googleapis.com/maps/api/place/nearbysearch/json?key=SECRET"],
        "missing_fields": [],
    }


def test_html_generation_and_no_api_key_leakage():
    profile = _profile()
    scored = score_profile(profile)
    html = render_html_report({
        "context": {"mode": "metro_comparison"},
        "candidates": [{"rank": 1, "profile": profile, "scored": scored}],
    })
    assert "candidate-card" in html
    assert "Source audit" in html
    assert "SECRET" not in html
    assert "photo_reference" in html
    assert "estimated" in html.lower()


def test_report_safety_strips_token_like_query_params():
    url = strip_sensitive_query_params(
        "https://example.com/classes?utm_source=x&rwg_token=abc&api_key=secret&id=1"
    )
    assert "rwg_token" not in url
    assert "api_key" not in url
    assert "utm_source=x" in url
    assert "id=1" in url


def test_candidate_dedup_keeps_best_nearby_candidate():
    p1 = _profile("near-low", lat=37.0, lon=-121.0)
    p2 = _profile("near-high", lat=37.001, lon=-121.001)
    p3 = _profile("far", lat=37.2, lon=-121.2)
    deduped = deduplicate_ranked_candidates(
        [(p1, {"site_score": 40}), (p2, {"site_score": 80}), (p3, {"site_score": 60})],
        min_distance_miles=0.5,
    )
    ids = [p["candidate_id"] for p, _ in deduped]
    assert "near-high" in ids
    assert "near-low" not in ids
    assert "far" in ids


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "text/html"}

    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text


class _FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if len(self.urls) == 1:
            return _FakeResponse(
                url,
                '<a href="/classes">Classes</a><p>Call 555-222-3333</p>',
            )
        return _FakeResponse(
            url,
            "Class schedule Register now $89 AHA BLS CPR certification Spanish",
        )


def test_competitor_website_analysis_with_mocked_html():
    session = _FakeSession()
    result = analyze_website("https://training.example.com", session=session)
    assert result["checked"] is True
    assert len(result["pages_checked"]) == 2
    assert "online_booking" in result["detected"]
    assert "class_schedule" in result["detected"]
    assert "pricing" in result["detected"]
    assert "multilingual_support" in result["detected"]
    assert "certification_keywords" in result["detected"]


def test_accessibility_scoring_uses_real_proxy_signals():
    profile = _profile()
    score = compute_accessibility_score(
        profile["counts_by_bucket"],
        profile["accessibility"],
    )
    fallback = compute_accessibility_score(profile["counts_by_bucket"])
    assert score > fallback
    assert score > 50


def test_source_audit_output_in_markdown():
    profile = _profile()
    scored = score_profile(profile)
    # The detailed style keeps the full per-candidate tables.
    md = render_markdown_report("Testville", "CA", 2.0, [(profile, scored)],
                                report_style="detailed")
    assert "Source audit (compact)" in md
    assert "Job posting certification demand" in md
    assert "platform_api" in md
    assert "SECRET" not in md


def test_rent_override_matching(tmp_path, monkeypatch):
    override = tmp_path / "rent_overrides.csv"
    override.write_text(
        "city,state,latitude,longitude,radius_miles,rent_per_sqft_annual,source_url,notes\n"
        "Testville,CA,37.0,-121.0,2,36,https://broker.example/rent,broker comp\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(real_estate, "OVERRIDE_FILE", override)
    block = real_estate.collect_real_estate("Testville", "CA", 37.01, -121.01)
    assert block["values"]["rent_per_sqft_annual"] == 36.0
    # Optional columns absent in the CSV stay None — backward compatibility.
    assert block["values"]["vacancy_rate_pct"] is None
    assert block["values"]["median_commercial_lease_term_months"] is None
    rent = compute_rent_score({"real_estate": block})
    assert rent.rent_score is not None
    assert rent.rent_data_confidence == "manual_override"


def test_rent_override_optional_columns_populate_when_present(tmp_path, monkeypatch):
    override = tmp_path / "rent_overrides.csv"
    # New schema: vacancy_rate_pct and median_commercial_lease_term_months are
    # optional. When present, they propagate through and source.fields lists them.
    override.write_text(
        "city,state,latitude,longitude,radius_miles,rent_per_sqft_annual,"
        "source_url,notes,vacancy_rate_pct,median_commercial_lease_term_months\n"
        "Testville,CA,37.0,-121.0,2,36,https://broker.example/rent,"
        "broker comp,7.5,60\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(real_estate, "OVERRIDE_FILE", override)
    block = real_estate.collect_real_estate("Testville", "CA", 37.01, -121.01)
    assert block["values"]["vacancy_rate_pct"] == 7.5
    assert block["values"]["median_commercial_lease_term_months"] == 60.0
    populated = block["sources"][0]["fields"]
    assert "vacancy_rate_pct" in populated
    assert "median_commercial_lease_term_months" in populated


def test_job_postings_csv_scanner_and_score(tmp_path, monkeypatch):
    postings = tmp_path / "job_postings.csv"
    postings.write_text(
        "city,state,latitude,longitude,radius_miles,employer,title,description,source_url,posted_at,notes\n"
        "Testville,CA,37.0,-121.0,2,General Hospital,RN,"
        "\"BLS required. CPR certification from AHA preferred.\","
        "https://jobs.example/rn?rwg_token=SECRET,2026-05-01,public posting\n"
        "Testville,CA,37.0,-121.0,2,Kids Care,Preschool Teacher,"
        "\"CPR and First Aid required for childcare role.\","
        "https://jobs.example/teacher,2026-05-02,public posting\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(job_postings, "JOB_POSTINGS_FILE", postings)
    block = job_postings.collect_job_posting_demand("Testville", "CA", 37.01, -121.01)
    values = block["values"]
    assert values["active_postings_count"] == 2
    assert values["certification_postings_count"] == 2
    assert values["bls_count"] == 1
    assert values["cpr_count"] == 2
    assert "rwg_token" not in block["sample_postings"][0]["source_url"]
    scored = compute_job_demand_score(block)
    assert scored.score is not None
    assert scored.data_confidence == "manual_csv"
    assert scored.certification_postings_count == 2


def test_job_demand_unknown_when_no_csv(tmp_path, monkeypatch):
    missing = tmp_path / "missing.csv"
    monkeypatch.setattr(job_postings, "JOB_POSTINGS_FILE", missing)
    block = job_postings.collect_job_posting_demand("Testville", "CA", 37.0, -121.0)
    scored = compute_job_demand_score(block)
    assert scored.score is None
    assert scored.data_confidence == "unknown"


def test_job_demand_renders_to_csv_and_html():
    profile = _profile()
    profile["job_demand"] = {
        "values": {
            "active_postings_count": 3,
            "certification_postings_count": 2,
            "bls_count": 2,
            "cpr_count": 2,
            "first_aid_count": 1,
            "acls_count": 0,
            "pals_count": 0,
            "aha_red_cross_count": 1,
            "healthcare_role_count": 2,
            "emt_role_count": 0,
            "cna_role_count": 0,
            "caregiver_role_count": 1,
            "dental_role_count": 0,
            "childcare_role_count": 0,
            "unique_employers_count": 2,
        },
        "top_employers": [{"employer": "General Hospital", "posting_count": 2}],
        "sample_postings": [{
            "employer": "General Hospital",
            "title": "RN",
            "source_url": "https://jobs.example/rn?token=SECRET&id=1",
            "posted_at": "2026-05-01",
            "distance_miles": 0.2,
            "certification_signals": ["bls", "cpr"],
            "role_signals": ["healthcare_role"],
        }],
        "sources": [],
    }
    scored = score_profile(profile)
    row = candidate_to_row(profile, scored)
    assert row["job_demand_data_confidence"] == "manual_csv"
    assert row["job_active_postings_count"] == 3
    assert "General Hospital" in row["job_top_employers"]
    html = render_html_report({
        "context": {"mode": "single_address"},
        "candidates": [{"rank": 1, "profile": profile, "scored": scored}],
    })
    assert "Job posting certification demand" in html
    assert "General Hospital" in html
    assert "token=" not in html


def test_cli_argument_parsing():
    args = full_pipeline.parse_args([
        "--mode", "metro_comparison",
        "--cities", "targets.txt",
        "--state", "CA",
        "--html-output", "report.html",
        "--skip-competitor-websites",
    ])
    assert args.mode == "metro_comparison"
    assert args.html_output == "report.html"
    assert args.analyze_competitor_websites is False


def test_metro_comparison_mode_and_full_mocked_phase2_run(tmp_path, monkeypatch):
    targets = tmp_path / "targets.txt"
    targets.write_text("Area A, CA\nArea B, CA\n", encoding="utf-8")
    md_path = tmp_path / "metro.md"
    csv_path = tmp_path / "scored.csv"
    json_path = tmp_path / "scored.json"
    html_path = tmp_path / "report.html"

    def fake_geocode(city, state):
        return LatLon(37.0, -121.0) if city == "Area A" else LatLon(37.2, -121.2)

    def fake_build(client, city, state, latitude, longitude, radius_miles, **kwargs):
        return _profile(candidate_id=f"{city}-001", city=city, lat=latitude, lon=longitude)

    monkeypatch.setattr(full_pipeline, "GooglePlacesClient", lambda **kw: object())
    monkeypatch.setattr(full_pipeline, "geocode_city", fake_geocode)
    monkeypatch.setattr(full_pipeline, "build_area_profile", fake_build)
    monkeypatch.setattr(sys, "argv", [
        "full_pipeline.py",
        "--mode", "metro_comparison",
        "--cities", str(targets),
        "--state", "CA",
        "--radius-miles", "2",
        "--output", str(md_path),
        "--csv-output", str(csv_path),
        "--json-output", str(json_path),
        "--html-output", str(html_path),
        "--dashboard-json", str(tmp_path / "latest_report.json"),
        "--skip-competitor-websites",
    ])

    assert full_pipeline.run() == 0
    assert md_path.exists()
    assert csv_path.exists()
    assert json_path.exists()
    assert html_path.exists()
    # Dashboard JSON is written alongside the HTML report (web_app.py source).
    assert (tmp_path / "latest_report.json").exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["context"]["mode"] == "metro_comparison"
    assert payload["context"]["city_rankings"]
    assert "City / area ranking" in md_path.read_text(encoding="utf-8")
    assert "candidate-card" in html_path.read_text(encoding="utf-8")
