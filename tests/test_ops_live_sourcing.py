"""
Tests for live external instructor-lead sourcing.

No real network calls — the Yelp/Google source helpers are monkeypatched, so
these pin the merge/dedupe/filter/rank/honesty logic, not the collectors
(which have their own tests).
"""
from __future__ import annotations

from app.ops import live_sourcing as ls
from app.ops.models import scrub_sensitive


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def test_strong_name_detects_cpr_signals():
    assert ls._strong_name("Beat CPR Training Center") is True
    assert ls._strong_name("American Red Cross") is True
    assert ls._strong_name("Kismet") is False


def test_is_allcpr_filters_own_listings():
    assert ls._is_allcpr("AllCPR") is True
    assert ls._is_allcpr("ALL CPR") is True
    assert ls._is_allcpr("Beat CPR Training") is False


def test_lead_marks_signal_only_and_contactable():
    lead = ls._lead("Beat CPR", "google_places", phone="408-345-3588")
    assert lead["credential_status"] == "SIGNAL_ONLY"   # a business is a lead
    assert lead["lead_type"] == "CPR_BUSINESS_OWNER"
    assert lead["contactable"] is True
    no_contact = ls._lead("Some Provider", "competitor_data")
    assert no_contact["contactable"] is False


def test_dedupe_merges_same_business_across_sources():
    leads = [
        ls._lead("Beat CPR Training Center", "google_places",
                 phone="408-345-3588"),
        ls._lead("Beat CPR Training Center", "yelp",
                 url="https://yelp.com/beat-cpr"),
    ]
    out = ls._dedupe(leads)
    assert len(out) == 1
    # Merged: kept phone from google, url from yelp, combined source.
    assert out[0]["phone"] == "408-345-3588"
    assert out[0]["url"] == "https://yelp.com/beat-cpr"
    assert "+" in out[0]["source"]


# --------------------------------------------------------------------------
# find_live_instructor_leads (mocked sources)
# --------------------------------------------------------------------------
def test_find_leads_merges_filters_and_ranks(monkeypatch):
    monkeypatch.setattr(ls, "yelp_configured", lambda: True)
    monkeypatch.setattr(ls, "GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(ls, "load_zip_centroids",
                        lambda: {"95112": (37.34, -121.88)})
    monkeypatch.setattr(ls, "_make_cache", lambda: None)
    monkeypatch.setattr(ls, "_yelp_leads", lambda *a, **k: [
        ls._lead("Beat CPR Training Center", "yelp", phone="408-345-3588",
                 distance_miles=3.9, rating=4.8, review_count=40),
    ])
    monkeypatch.setattr(ls, "_google_leads", lambda *a, **k: [
        ls._lead("Beat CPR Training Center", "google_places",
                 url="http://beatcpr.com", distance_miles=3.9),   # dup
        ls._lead("CPR Certification", "google_places", phone="408-351-1153",
                 distance_miles=2.0),
        ls._lead("AllCPR", "google_places", phone="408-443-3055",
                 distance_miles=2.1),                              # own listing
        ls._lead("Far Away CPR", "google_places", phone="1", distance_miles=99),
    ])
    monkeypatch.setattr(ls, "_competitor_provider_leads", lambda *a, **k: [
        ls._lead("Safety Training Seminars", "competitor_data"),
    ])
    r = ls.find_live_instructor_leads("95112", radius_miles=6.0, limit=10)
    names = [l["name"] for l in r["leads"]]
    # ALLCPR filtered, far one dropped, Beat CPR deduped once.
    assert "AllCPR" not in names
    assert "Far Away CPR" not in names
    assert names.count("Beat CPR Training Center") == 1
    assert "Safety Training Seminars" in names
    # Contactable + close leads rank above the no-contact provider.
    assert r["leads"][0]["contactable"] is True
    assert r["count"] == len(r["leads"])


def test_find_leads_no_keys_uses_competitor_data_only(monkeypatch):
    monkeypatch.setattr(ls, "yelp_configured", lambda: False)
    monkeypatch.setattr(ls, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(ls, "load_zip_centroids", lambda: {})
    monkeypatch.setattr(ls, "_make_cache", lambda: None)
    monkeypatch.setattr(ls, "_competitor_provider_leads", lambda *a, **k: [
        ls._lead("Safety Training Seminars", "competitor_data"),
    ])
    r = ls.find_live_instructor_leads("99999")
    assert r["configured"] == {"yelp": False, "google": False}
    assert r["sources_used"] == ["competitor_data"]
    assert r["note"] and "not configured" in r["note"]
    assert r["leads"][0]["name"] == "Safety Training Seminars"


def test_competitor_provider_leads_reads_context(monkeypatch):
    monkeypatch.setattr(ls, "competitor_context", lambda z: {
        "courses": [
            {"providers": ["Vital Connect", "Safety Training Seminars"],
             "sample_locations": ["San Jose, CA"]},
        ]})
    out = ls._competitor_provider_leads("95112")
    assert {l["name"] for l in out} == {"Vital Connect",
                                        "Safety Training Seminars"}
    assert all(l["source"] == "competitor_data" for l in out)
    assert all(l["credential_status"] == "SIGNAL_ONLY" for l in out)


def test_live_leads_scrub_clean(monkeypatch):
    monkeypatch.setattr(ls, "yelp_configured", lambda: False)
    monkeypatch.setattr(ls, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(ls, "_make_cache", lambda: None)
    monkeypatch.setattr(ls, "_competitor_provider_leads", lambda *a, **k: [
        ls._lead("Provider X", "competitor_data")])
    r = ls.find_live_instructor_leads("95112")
    assert "door_code" not in str(scrub_sensitive(r))
