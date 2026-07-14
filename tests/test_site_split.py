"""area_score vs gated site_score, business_feasibility, competition_detail."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import commercial_listings as cl  # noqa: E402
from app.collectors.commercial_listings import CommercialOverride  # noqa: E402
from app.scoring.site_score import score_profile  # noqa: E402
from tests.test_scoring import _synthetic_profile  # noqa: E402


def test_unvalidated_candidate_withholds_site_score(monkeypatch):
    monkeypatch.setattr(cl, "_CACHE", [])
    out = score_profile(_synthetic_profile())
    assert isinstance(out["area_score"], (int, float))
    assert out["site_score"] is None
    assert out["site_score_status"] == "not_validated"
    assert out["candidate_type"] == "landmark_proxy"
    assert out["validation_flags"]["lease_ready"] is False
    # honest verdict, never "lease-ready" on demand alone
    assert out["executive_state"] in (
        "Recommended for field validation", "Recommended for listing search",
        "Not recommended",
    )
    # feasibility + competition_detail always attached
    assert "business_feasibility" in out and "competition_detail" in out
    assert out["business_feasibility"]["breakeven_students_per_month"] is not None
    assert len(out["next_actions"]) == 8


def test_validated_override_unlocks_site_score(monkeypatch):
    ov = CommercialOverride(
        candidate_id="TEST-001", validation_status="validated",
        asking_rent=28.0, square_feet=1600,
        parking_notes="12 dedicated spaces", broker_notes="ground floor, signage",
    )
    monkeypatch.setattr(cl, "_CACHE", [ov])
    out = score_profile(_synthetic_profile())
    assert out["candidate_type"] == "verified_commercial_listing"
    assert out["site_score_status"] == "validated"
    assert isinstance(out["site_score"], (int, float))
    bf = out["business_feasibility"]
    assert bf["rent_score"] is not None          # from cited asking_rent
    assert bf["parking_score"] is not None
    assert bf["classroom_fit_score"] is not None
    assert bf["lease_readiness_score"] is not None
    assert out["validation_flags"]["lease_ready"] is True
    assert out["validation_flags"]["rent_validated"] is True


def test_competition_detail_bands_and_pressure(monkeypatch):
    monkeypatch.setattr(cl, "_CACHE", [])
    out = score_profile(_synthetic_profile())
    cd = out["competition_detail"]
    # buckets 1/3/5 = 0/1/2 → bands 0,1,1
    assert cd["band_0_1_mi"] == 0
    assert cd["band_1_3_mi"] == 1
    assert cd["band_3_5_mi"] == 1
    assert cd["competition_pressure_band"] in (
        "Low", "Medium", "High", "Extreme", "Unknown",
    )
