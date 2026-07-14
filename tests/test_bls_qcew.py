"""BLS QCEW collector tests — CSV parsing + healthcare-record extraction.

Network is mocked; these verify the CSV→values transform and the
avg-weekly-wage derivation that the slim CSV requires.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import bls_or_labor as bls  # noqa: E402

# A trimmed QCEW area CSV: a non-healthcare row + the NAICS 62 private row.
_FAKE_CSV_ROWS = [
    {
        "area_fips": "06075", "own_code": "5", "industry_code": "44",
        "month1_emplvl": "1000", "month2_emplvl": "1010", "month3_emplvl": "1020",
        "total_qtrly_wages": "13000000",
    },
    {
        "area_fips": "06075", "own_code": "5", "industry_code": "62",
        "month1_emplvl": "80000", "month2_emplvl": "82000", "month3_emplvl": "84000",
        "total_qtrly_wages": "1419600000",  # ~ $1300/wk at 84k emp
        "lq_month3_emplvl": "1.10",
    },
]


def test_healthcare_record_picks_naics_62_private():
    rec = bls._healthcare_record(_FAKE_CSV_ROWS)
    assert rec is not None
    assert rec["industry_code"] == "62"
    assert rec["own_code"] == "5"


def test_healthcare_record_none_when_absent():
    rows = [{"industry_code": "44", "own_code": "5"}]
    assert bls._healthcare_record(rows) is None


def test_collect_labor_parses_and_derives_wage(monkeypatch):
    # Force a resolved county and a single successful fetch.
    monkeypatch.setattr(bls, "_coords_to_county", lambda lat, lon: ("06", "075"))
    with patch.object(bls, "_fetch_qcew", return_value=_FAKE_CSV_ROWS):
        out = bls.collect_labor(37.7749, -122.4194)
    vals = out["values"]
    assert vals["healthcare_employment_count"] == 84000
    # avg wkly wage = total_qtrly_wages / avg_emp / 13
    # avg_emp = (80000+82000+84000)/3 = 82000 ; 1419600000/82000/13 ≈ 1331.6
    assert 1300 <= vals["avg_weekly_wage_healthcare"] <= 1360
    assert vals["healthcare_employment_lq"] == 1.10
    assert out["sources"][0]["name"].startswith("BLS QCEW")


def test_collect_labor_stub_when_county_unresolved(monkeypatch):
    monkeypatch.setattr(bls, "_coords_to_county", lambda lat, lon: None)
    out = bls.collect_labor(0.0, 0.0)
    assert all(v is None for v in out["values"].values())
    assert "not yet integrated" in out["sources"][0]["name"]


def test_collect_labor_stub_when_all_quarters_miss(monkeypatch):
    monkeypatch.setattr(bls, "_coords_to_county", lambda lat, lon: ("06", "075"))
    with patch.object(bls, "_fetch_qcew", return_value=None):
        out = bls.collect_labor(37.0, -122.0)
    assert all(v is None for v in out["values"].values())


def test_url_uses_csv_not_json():
    url = bls._qcew_url(2025, 1, "06075")
    assert url.endswith(".csv")
    assert "06075" in url
