"""Tests for the national modeled-demand QA report."""
from __future__ import annotations

from scripts.qa_national_demand import build_qa_report, score_distribution


def _payload():
    return {
        "layer": "modeled_national_demand",
        "rows": [
            {
                "zip": "95112",
                "lat": 37.33,
                "lng": -121.88,
                "overall": 82,
                "bls_demand": 85,
                "cpr_demand": 79,
                "population": 56000,
                "population_density": 9000,
                "median_income": 110000,
                "healthcare_employment_share": 0.16,
                "data_confidence": "ok",
                "recommended_next_action": "Run light field test before committing.",
                "risk_flags": ["modeled_only"],
            },
            {
                "zip": "00001",
                "lat": None,
                "lng": None,
                "overall": 75,
                "bls_demand": 72,
                "cpr_demand": 78,
                "population": 1200,
                "population_density": 40,
                "median_income": None,
                "healthcare_employment_share": None,
                "data_confidence": "partial",
                "recommended_next_action": "Low priority unless automated enrichment signals improve.",
                "risk_flags": ["low_population"],
            },
        ],
    }


def test_score_distribution_required_stats():
    dist = score_distribution(_payload()["rows"], "overall")
    assert dist["count"] == 2
    assert dist["min"] == 75
    assert dist["max"] == 82
    assert dist["above_70"] == 2
    assert dist["below_40"] == 0
    assert dist["p10"] is not None


def test_build_qa_report_has_required_keys_and_outliers():
    gaz = {
        "95112": {"lat": 37.33, "lng": -121.88, "land_sqmi": 6.0},
        "00001": {"lat": 1, "lng": 1, "land_sqmi": 10.0},
        "99999": {"lat": 2, "lng": 2, "land_sqmi": 0.0},
    }
    acs = {
        "95112": {"population": 56000},
        "00001": {"population": 1200},
        "99999": {"population": None},
    }
    report = build_qa_report(_payload(), gazetteer=gaz, acs_by_zip=acs)
    assert report["input_counts"]["gazetteer_rows_loaded"] == 3
    assert report["input_counts"]["acs_rows_loaded"] == 3
    assert report["input_counts"]["matched_zip_zcta_rows"] == 3
    assert report["output_counts"]["total_modeled_rows"] == 2
    assert report["output_counts"]["rows_with_lat_lng"] == 1
    assert report["output_counts"]["rows_with_land_area"] == 2
    assert report["output_counts"]["rows_omitted_from_scoring"] == 1
    assert "overall" in report["score_distributions"]
    assert report["top_zips"]["overall"][0]["zip"] == "95112"
    assert report["suspicious_outliers"]["high_score_low_population"]
    assert report["suspicious_outliers"]["missing_coordinates"]
