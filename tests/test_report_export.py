"""Tests for the dashboard JSON export (app/reports/report_export.py)."""
from __future__ import annotations

import json

from app.reports import report_export as rx

REQUIRED_ZIP_FIELDS = (
    "zip", "lat", "lng", "demand_score", "class_count",
    "arc_cpr_students", "arc_bls_students", "aha_bls_students",
    "avg_students", "fill_rate", "confidence", "recommendation", "reason",
    "missing_centroid", "course_scores",
)


def _sample_payload() -> dict:
    """A minimal report payload shaped like scored_locations.json."""
    return {
        "context": {
            "mode": "city",
            "cities": ["San Jose, CA"],
            "default_state": "CA",
            "ai_summary": {"text": "A short narrative about the area."},
            "course_performance": {
                "total_classes": 282,
                "course_types": [
                    {"course_type": "arc_cpr", "label": "ARC CPR", "total_students": 835},
                    {"course_type": "aha_bls", "label": "AHA BLS", "total_students": 673},
                    {"course_type": "arc_bls", "label": "ARC BLS", "total_students": 249},
                ],
            },
            "zip_demand_report": {
                "total_zips": 2,
                "total_classes": 50,
                "rows": [
                    {
                        "zip": "95112", "demand_score": 88.8, "strength": "Strong",
                        "classes": 41, "total_students": 421, "month_span": 4,
                        "arc_cpr_students": 294, "arc_bls_students": 127,
                        "aha_bls_students": 0, "avg_students": 10.27,
                        "fill_rate": 85.4, "centroid_present": True,
                        "lat": 37.33, "lng": -121.88,
                        "latest_class_date": "2026-05-30",
                    },
                    {
                        # No centroid, missing several numeric fields on purpose.
                        "zip": "00000", "demand_score": 12.0, "strength": "Weak",
                        "classes": 0, "centroid_present": False,
                    },
                ],
            },
        },
        "report_interpretation": {
            "executive_verdict": {
                "best_candidate": "San Jose — sixth street corridor",
                "executive_state": "Recommended for field validation",
                "verdict": "Mixed — needs more data",
                "expansion_readiness": "Weak",
                "why_it_matters": "Strong demand near the anchor.",
                "biggest_risk": "No commercial storefront anchor identified.",
                "best_strategy": "Target nursing-student certification.",
                "confidence": "Very high (85/100)",
                "before_leasing": "Validate parking and real class demand.",
            },
            "next_actions": [
                "Validate rent and parking.",
                "Contact nearby healthcare schools.",
                "Run a small paid-search test before signing a lease.",
            ],
        },
        "candidates": [
            {
                "rank": 1,
                "profile": {
                    "area_display_name": "San Jose — sixth street corridor",
                    "city": "San Jose", "state": "CA",
                    "latitude": 37.33874, "longitude": -121.885,
                    "candidate_source": "city",
                    "source_names": ["Google Places API", "US Census Bureau"],
                    "anchor": {"name": "Doc's Office", "category": "medical",
                               "formatted_address": "sixth street, San Jose, CA, 95112"},
                },
                "scored": {
                    "area_score": 56.4, "site_score": None,
                    "executive_state": "Recommended for field validation",
                    "tier": "C", "tier_label": "Mixed / needs more data",
                    "sub_scores": {"confidence_score": 85},
                },
                "interpretation": {
                    "expansion_readiness": {"readiness": "Weak"},
                    "warnings": ["No commercial storefront anchor identified."],
                },
            },
            {
                # Candidate with no coordinates — must not crash.
                "rank": 2,
                "profile": {"area_display_name": "No-coords area",
                            "anchor": {}, "source_names": []},
                "scored": {"area_score": 40.0, "tier": "D"},
                "interpretation": {},
            },
        ],
    }


def test_payload_has_top_level_shape():
    out = rx.build_latest_report_payload(_sample_payload())
    for key in ("generated_at", "mode", "city", "executive_summary",
                "zip_demand", "candidates", "metadata"):
        assert key in out
    assert out["city"] == "San Jose, CA"
    assert out["mode"] == "city"


def test_executive_summary_fields():
    es = rx.build_latest_report_payload(_sample_payload())["executive_summary"]
    assert es["best_area"] == "San Jose — sixth street corridor"
    assert es["recommendation"] == "Recommended for field validation"
    assert es["area_score"] == 56.4
    assert es["best_course"] == "ARC CPR"  # highest total_students
    assert es["confidence"] == "Very high (85/100)"
    assert es["expansion_readiness"] == "Weak"
    assert es["biggest_risk"]
    assert es["before_leasing"]
    assert len(es["next_actions"]) == 3
    assert es["long_summary"] == "A short narrative about the area."
    assert es["data_confidence_note"]


def test_zip_rows_required_fields_and_types():
    out = rx.build_latest_report_payload(_sample_payload())
    rows = out["zip_demand"]
    assert isinstance(rows, list) and len(rows) == 2
    for row in rows:
        for field in REQUIRED_ZIP_FIELDS:
            assert field in row, f"missing {field}"
        assert isinstance(row["course_scores"], dict)
        for k in ("overall", "aha_bls", "arc_bls", "arc_cpr"):
            assert k in row["course_scores"]


def test_zip_rows_ranked_by_demand():
    rows = rx.build_latest_report_payload(_sample_payload())["zip_demand"]
    assert rows[0]["zip"] == "95112"
    assert rows[0]["rank"] == 1
    assert rows[1]["rank"] == 2


def test_missing_centroid_and_missing_numerics_default_safely():
    rows = rx.build_latest_report_payload(_sample_payload())["zip_demand"]
    weak = next(r for r in rows if r["zip"] == "00000")
    assert weak["missing_centroid"] is True
    assert weak["lat"] is None and weak["lng"] is None
    # Missing course counts default to 0, never crash.
    assert weak["arc_cpr_students"] == 0
    assert weak["aha_bls_students"] == 0
    assert weak["course_scores"]["arc_cpr"] == 0
    assert weak["recommendation"] == "Lower priority"
    assert weak["best_course"] is None


def test_course_heat_values_derivable():
    rows = rx.build_latest_report_payload(_sample_payload())["zip_demand"]
    strong = next(r for r in rows if r["zip"] == "95112")
    cs = strong["course_scores"]
    assert cs["overall"] == 88.8
    assert cs["arc_cpr"] == 294
    assert cs["arc_bls"] == 127
    assert cs["aha_bls"] == 0
    assert strong["best_course"] == "ARC CPR"


def test_candidate_rows_and_missing_coords():
    out = rx.build_latest_report_payload(_sample_payload())
    cands = out["candidates"]
    assert len(cands) == 2
    best = cands[0]
    assert best["name"] == "San Jose — sixth street corridor"
    assert best["zip"] == "95112"
    assert best["area_score"] == 56.4
    assert best["best_course"] == "ARC CPR"
    assert best["confidence"].startswith("Very high")
    # Second candidate has no coords — present but lat/lng None.
    assert cands[1]["lat"] is None and cands[1]["lng"] is None


def test_metadata_counts():
    meta = rx.build_latest_report_payload(_sample_payload())["metadata"]
    assert meta["zip_count"] == 2
    assert meta["candidate_count"] == 2
    assert meta["missing_centroid_count"] == 1
    assert "Google Places API" in meta["data_sources"]
    assert meta["warnings"]  # missing centroid + missing-coords candidate
    assert meta["notes"]


def test_empty_payload_does_not_crash():
    out = rx.build_latest_report_payload({})
    assert out["zip_demand"] == []
    assert out["candidates"] == []
    assert out["executive_summary"]["best_area"] is None


def test_write_and_load_roundtrip(tmp_path):
    path = tmp_path / "latest_report.json"
    written = rx.write_latest_report_json(_sample_payload(), output_path=path)
    assert written == path
    loaded = rx.load_latest_report_json(path)
    assert loaded["zip_demand"][0]["zip"] == "95112"
    # Round-trips as valid JSON.
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "city"


def test_load_missing_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        rx.load_latest_report_json(tmp_path / "nope.json")
