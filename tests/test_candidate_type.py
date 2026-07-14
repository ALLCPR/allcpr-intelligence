"""Candidate-type classification + commercial-override loader."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import commercial_listings as cl  # noqa: E402
from app.collectors.commercial_listings import CommercialOverride  # noqa: E402
from app.scoring import candidate_type as ct  # noqa: E402


def _profile(viable=True, commercial=False, cid="c1", name="Foo", addr="1 Main St"):
    return {
        "candidate_id": cid,
        "candidate_name": name,
        "city": "San Jose",
        "anchor": {"name": name, "formatted_address": addr},
        "viability": {
            "viable": viable,
            "commercial_anchor": commercial,
            "commercial_reason": "store" if commercial else "",
            "reason": "" if viable else "non-commercial place type: transit_station",
        },
    }


# --------------------------- candidate_type ------------------------------- #

def test_invalid_when_not_viable():
    r = ct.classify(_profile(viable=False))
    assert r.candidate_type == ct.INVALID_OR_LOW_CONFIDENCE
    assert r.is_site_candidate is False


def test_landmark_proxy_when_viable_not_commercial():
    r = ct.classify(_profile(viable=True, commercial=False))
    assert r.candidate_type == ct.LANDMARK_PROXY
    assert r.is_site_candidate is False


def test_commercial_area_proxy_when_commercial_signal():
    r = ct.classify(_profile(viable=True, commercial=True))
    assert r.candidate_type == ct.COMMERCIAL_AREA_PROXY
    assert r.is_site_candidate is False


def test_verified_listing_unlocks_site_candidate():
    ov = CommercialOverride(candidate_id="c1", validation_status="validated")
    r = ct.classify(_profile(), override=ov)
    assert r.candidate_type == ct.VERIFIED_COMMERCIAL_LISTING
    assert r.is_site_candidate is True


def test_pending_override_does_not_verify():
    ov = CommercialOverride(candidate_id="c1", validation_status="pending")
    r = ct.classify(_profile(commercial=True), override=ov)
    # Falls through to commercial_area_proxy (override not validated).
    assert r.candidate_type == ct.COMMERCIAL_AREA_PROXY


def test_demand_level_confirmed_from_notes():
    ov = CommercialOverride(candidate_id="c1", validation_status="validated",
                            broker_notes="real enrollment data attached")
    r = ct.classify(_profile(), override=ov)
    assert r.demand_validation_level == "confirmed"


# --------------------------- override loader ------------------------------ #

def test_lookup_by_candidate_id(monkeypatch):
    table = [CommercialOverride(candidate_id="cX", validation_status="validated",
                                asking_rent=30.0, square_feet=1500)]
    monkeypatch.setattr(cl, "_CACHE", table)
    ov = cl.lookup_override({"candidate_id": "cX"})
    assert ov is not None and ov.is_validated and ov.asking_rent == 30.0


def test_lookup_by_address_substring(monkeypatch):
    table = [CommercialOverride(address_match="1631 N First St",
                                validation_status="validated")]
    monkeypatch.setattr(cl, "_CACHE", table)
    prof = {"candidate_id": "zzz",
            "anchor": {"formatted_address": "1631 N First St, San Jose, CA 95112"}}
    ov = cl.lookup_override(prof)
    assert ov is not None and ov.is_validated


def test_lookup_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(cl, "_CACHE", [])
    assert cl.lookup_override({"candidate_id": "nope"}) is None


def test_float_parsing_handles_dollar_and_commas():
    assert cl._to_float("$1,800") == 1800.0
    assert cl._to_float("") is None
    assert cl._to_float(None) is None
