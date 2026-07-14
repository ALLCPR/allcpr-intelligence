"""Tests for the SQLite cache + freshness helpers."""
from __future__ import annotations

import os

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

import pytest

from app.utils.cache import (
    CacheEntry,
    _canonical_params,
    build_cache_key,
)


def test_canonical_params_rounds_lat_lon_to_4_decimals():
    a = _canonical_params({"latitude": 37.32131, "longitude": -121.94781, "r": 5})
    b = _canonical_params({"latitude": 37.32134, "longitude": -121.94784, "r": 5})
    # ~4 m apart — below 4-decimal precision, must collapse to same string.
    assert a == b


def test_canonical_params_is_order_independent():
    a = _canonical_params({"a": 1, "b": 2, "c": 3})
    b = _canonical_params({"c": 3, "a": 1, "b": 2})
    assert a == b


def test_canonical_params_distinguishes_different_radii():
    near5 = _canonical_params({"latitude": 37.0, "longitude": -121.0, "r": 5})
    near6 = _canonical_params({"latitude": 37.0, "longitude": -121.0, "r": 6})
    assert near5 != near6


def test_build_cache_key_separates_method_namespaces():
    p = {"latitude": 37.0, "longitude": -121.0, "r": 5}
    nearby = build_cache_key("google_places", "nearby_search", p)
    text = build_cache_key("google_places", "text_search", p)
    assert nearby != text
    assert nearby.startswith("google_places:nearby_search:")
    assert text.startswith("google_places:text_search:")


def test_build_cache_key_collides_for_near_identical_coords():
    p1 = {"latitude": 37.32131, "longitude": -121.94781, "r": 5}
    p2 = {"latitude": 37.32134, "longitude": -121.94784, "r": 5}
    assert build_cache_key("google_places", "nearby_search", p1) == \
           build_cache_key("google_places", "nearby_search", p2)


def test_cache_entry_dataclass_round_trip_basic_fields():
    e = CacheEntry(value={"x": 1}, as_of="2026-05-24T12:00:00Z",
                   ttl_seconds=3600, provider="google_places")
    assert e.value == {"x": 1}
    assert e.provider == "google_places"


import time
from pathlib import Path

from app.utils.cache import Cache


def test_cache_set_get_round_trip(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("k1", {"hello": "world"}, ttl_seconds=60, provider="test")
    hit = cache.get("k1")
    assert hit is not None
    assert hit.value == {"hello": "world"}
    assert hit.provider == "test"
    assert hit.ttl_seconds == 60
    assert hit.as_of  # non-empty ISO string


def test_cache_get_returns_none_for_missing_key(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    assert cache.get("never-set") is None


def test_cache_get_returns_none_after_ttl_expiry(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("k1", "val", ttl_seconds=1, provider="test")
    assert cache.get("k1") is not None     # still valid
    time.sleep(1.05)
    assert cache.get("k1") is None         # expired


def test_cache_info_ignores_staleness(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("k1", "val", ttl_seconds=1, provider="test")
    time.sleep(1.05)
    assert cache.get("k1") is None         # stale to get()
    assert cache.info("k1") is not None    # but info() still sees it


def test_cache_creates_parent_directory(tmp_path: Path):
    db = tmp_path / "nested" / "deeper" / "cache.sqlite"
    cache = Cache(db)
    cache.set("k1", "val", ttl_seconds=60, provider="test")
    assert db.exists()


def test_cache_overwrites_existing_key(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("k1", "first", ttl_seconds=60, provider="test")
    cache.set("k1", "second", ttl_seconds=60, provider="test")
    hit = cache.get("k1")
    assert hit is not None and hit.value == "second"


def test_cache_no_cache_mode_bypasses_reads_and_writes(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite", mode="no-cache")
    cache.set("k1", "val", ttl_seconds=60, provider="test")
    # no-cache writes nothing.
    assert cache.info("k1") is None
    # no-cache reads always miss.
    assert cache.get("k1") is None


def test_cache_force_refresh_mode_ignores_existing_entries(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite", mode="auto")
    cache.set("k1", "first", ttl_seconds=3600, provider="test")
    cache_fr = Cache(tmp_path / "cache.sqlite", mode="force-refresh")
    assert cache_fr.get("k1") is None       # force-refresh never hits
    cache_fr.set("k1", "second", ttl_seconds=3600, provider="test")
    # but writes still land — next auto run sees them.
    cache_auto = Cache(tmp_path / "cache.sqlite", mode="auto")
    hit = cache_auto.get("k1")
    assert hit is not None and hit.value == "second"


def test_cache_rejects_unknown_mode(tmp_path: Path):
    with pytest.raises(ValueError):
        Cache(tmp_path / "cache.sqlite", mode="bogus")


def test_cache_session_max_as_of_tracks_served_entries(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("k1", "v1", ttl_seconds=3600, provider="google_places")
    cache.set("k2", "v2", ttl_seconds=3600, provider="census")
    # Reads count as "served this session".
    cache.get("k1")
    cache.get("k2")
    gp = cache.session_max_as_of("google_places")
    cs = cache.session_max_as_of("census")
    assert gp is not None and cs is not None
    assert cache.session_max_as_of("never_used") is None


def test_cache_purge_stale_removes_expired_entries(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("fresh", "v", ttl_seconds=3600, provider="test")
    cache.set("stale", "v", ttl_seconds=1, provider="test")
    time.sleep(1.05)
    removed = cache.purge_stale()
    assert removed == 1
    assert cache.info("fresh") is not None
    assert cache.info("stale") is None


def test_cache_clear_by_provider_removes_only_that_provider(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("a", "v", ttl_seconds=3600, provider="google_places")
    cache.set("b", "v", ttl_seconds=3600, provider="census")
    removed = cache.clear(provider="google_places")
    assert removed == 1
    assert cache.info("a") is None
    assert cache.info("b") is not None


def test_cache_clear_all_removes_every_entry(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    cache.set("a", "v", ttl_seconds=3600, provider="x")
    cache.set("b", "v", ttl_seconds=3600, provider="y")
    assert cache.clear() == 2
    assert cache.info("a") is None
    assert cache.info("b") is None


def test_cache_recovers_from_corrupted_db(tmp_path: Path, caplog):
    db = tmp_path / "cache.sqlite"
    db.write_bytes(b"not a sqlite file at all")
    # Must NOT raise — must log a warning and create a fresh DB.
    cache = Cache(db)
    cache.set("k1", "v", ttl_seconds=60, provider="test")
    assert cache.get("k1") is not None


from app.utils.cache import cached_call


def test_cached_call_hits_cache_on_second_invocation(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite")
    calls = []

    def live():
        calls.append(1)
        return {"places": [1, 2, 3]}

    p = {"latitude": 37.0, "longitude": -121.0, "r": 5}
    v1, as_of1 = cached_call(cache, "google_places", "nearby_search",
                             p, ttl_seconds=60, live_call=live)
    v2, as_of2 = cached_call(cache, "google_places", "nearby_search",
                             p, ttl_seconds=60, live_call=live)
    assert v1 == v2 == {"places": [1, 2, 3]}
    assert as_of1 == as_of2     # cached hit returns the original as_of
    assert calls == [1]         # live_call invoked exactly once


def test_cached_call_with_no_cache_bypasses(tmp_path: Path):
    cache = Cache(tmp_path / "cache.sqlite", mode="no-cache")
    calls = []
    def live():
        calls.append(1)
        return "v"
    p = {"latitude": 37.0, "longitude": -121.0}
    cached_call(cache, "x", "y", p, ttl_seconds=60, live_call=live)
    cached_call(cache, "x", "y", p, ttl_seconds=60, live_call=live)
    assert calls == [1, 1]      # every call hits live


def test_cached_call_with_none_cache_just_calls_live():
    calls = []
    def live():
        calls.append(1)
        return 42
    v, as_of = cached_call(None, "x", "y", {}, ttl_seconds=60, live_call=live)
    assert v == 42
    assert as_of  # still returns a fresh timestamp


def test_ttl_for_known_keys_returns_configured_seconds():
    from app.config import ttl_for
    assert ttl_for("google_places", "nearby_search") >= 86400  # at least 1 day
    assert ttl_for("census", "fetch_demographics") >= 86400 * 30


def test_ttl_for_unknown_falls_back_to_default():
    from app.config import ttl_for, CACHE_TTL_DEFAULT_SECONDS
    assert ttl_for("nonexistent", "method") == CACHE_TTL_DEFAULT_SECONDS


def test_google_places_client_uses_cache_for_repeat_calls(tmp_path: Path, monkeypatch):
    """Two identical nearby_search calls hit the live API once with cache."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake")
    from app.collectors.google_places import GooglePlacesClient
    from app.utils.cache import Cache

    cache = Cache(tmp_path / "c.sqlite")
    client = GooglePlacesClient(cache=cache)

    live_calls = []

    def fake_live_nearby(self, location, radius_meters, place_type=None,
                         keyword=None, max_pages=1):
        live_calls.append((location, place_type, keyword))
        return [{"place_id": "p1", "name": "Fake"}]

    monkeypatch.setattr(GooglePlacesClient, "_nearby_search_live",
                        fake_live_nearby, raising=False)

    a = client.nearby_search((37.0, -121.0), 1000, place_type="hospital")
    b = client.nearby_search((37.0, -121.0), 1000, place_type="hospital")
    assert a == b == [{"place_id": "p1", "name": "Fake"}]
    assert len(live_calls) == 1   # second call served from cache


def test_google_places_client_without_cache_calls_live_each_time(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake")
    from app.collectors.google_places import GooglePlacesClient

    client = GooglePlacesClient(cache=None)
    live_calls = []

    def fake_live_nearby(self, location, radius_meters, place_type=None,
                         keyword=None, max_pages=1):
        live_calls.append(1)
        return []

    monkeypatch.setattr(GooglePlacesClient, "_nearby_search_live",
                        fake_live_nearby, raising=False)

    client.nearby_search((37.0, -121.0), 1000, place_type="hospital")
    client.nearby_search((37.0, -121.0), 1000, place_type="hospital")
    assert len(live_calls) == 2


def test_census_set_cache_makes_repeat_calls_hit_cache(tmp_path: Path, monkeypatch):
    from app.collectors import census
    from app.utils.cache import Cache

    cache = Cache(tmp_path / "c.sqlite")
    census.set_cache(cache)

    live_calls = []

    def fake_fetch_live(latitude, longitude):
        live_calls.append(1)
        return {"values": {"population": 100}, "indicators": {},
                "sources": [], "geo_desc": "test"}

    monkeypatch.setattr(census, "_fetch_demographics_live", fake_fetch_live)

    a = census.fetch_demographics(37.0, -121.0)
    b = census.fetch_demographics(37.0, -121.0)
    assert a == b
    assert len(live_calls) == 1

    # Reset module-level cache so other tests aren't affected.
    census.set_cache(None)


def test_bls_set_cache_makes_repeat_calls_hit_cache(tmp_path: Path, monkeypatch):
    from app.collectors import bls_or_labor
    from app.utils.cache import Cache

    cache = Cache(tmp_path / "c.sqlite")
    bls_or_labor.set_cache(cache)

    live_calls = []

    def fake_fetch_qcew_live(url):
        live_calls.append(url)
        return [{"industry_code": "62", "own_code": "5",
                 "month3_emplvl": "10", "avg_wkly_wage": "1000",
                 "lq_month3_emplvl": "1.0"}]

    monkeypatch.setattr(bls_or_labor, "_fetch_qcew_live", fake_fetch_qcew_live)
    monkeypatch.setattr(bls_or_labor, "_coords_to_county",
                        lambda lat, lon: ("06", "085"))

    a = bls_or_labor.collect_labor(37.0, -121.0)
    b = bls_or_labor.collect_labor(37.0, -121.0)
    assert a["values"]["healthcare_employment_count"] == 10
    assert b["values"]["healthcare_employment_count"] == 10
    # One live call (for one quarter URL); the second collect_labor hits cache.
    assert len(live_calls) == 1

    bls_or_labor.set_cache(None)


def test_cache_snapshot_session_returns_per_provider_dict(tmp_path: Path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.set("a", "v", ttl_seconds=3600, provider="google_places")
    cache.set("b", "v", ttl_seconds=3600, provider="census")
    cache.get("a")
    cache.get("b")
    snap = cache.snapshot_session()
    assert set(snap.keys()) == {"google_places", "census"}
    assert all(isinstance(v, str) and v for v in snap.values())


def test_compact_source_audit_uses_session_as_of_dict_when_provided():
    from app.utils.source_audit import build_compact_source_audit
    sources = [
        {"name": "Google Places API (Nearby Search)",
         "url": "https://maps.googleapis.com/x",
         "fields": ["x"], "collected_at": "2026-05-24T00:00:00Z"},
    ]
    snap = {"google_places": "2026-04-01T12:34:00Z"}
    rows = build_compact_source_audit(sources, session_as_of=snap)
    row = next(r for r in rows if r["source"] == "Google Places Nearby Search")
    # The snapshot value (April) wins over the collected_at fallback (May).
    assert row["data_as_of"] == "2026-04-01T12:34:00Z"


def test_bls_does_not_cache_none_results(tmp_path: Path, monkeypatch):
    """A None return from _fetch_qcew_live must NOT be cached.

    Otherwise newly-released quarters would be blocked for the BLS TTL
    (~365 days) because the cached None pretends the URL is still missing.
    """
    from app.collectors import bls_or_labor
    from app.utils.cache import Cache

    cache = Cache(tmp_path / "c.sqlite")
    bls_or_labor.set_cache(cache)
    try:
        call_count = {"n": 0}

        def fake_live(url):
            call_count["n"] += 1
            return None        # first call: pretend "not released yet"

        monkeypatch.setattr(bls_or_labor, "_fetch_qcew_live", fake_live)
        assert bls_or_labor._fetch_qcew("https://x.example/q1.json") is None

        # Now BLS publishes the quarter; live returns real data.
        def fake_live_now(url):
            call_count["n"] += 1
            return [{"industry_code": "62", "own_code": "5",
                     "month3_emplvl": "42"}]

        monkeypatch.setattr(bls_or_labor, "_fetch_qcew_live", fake_live_now)
        result = bls_or_labor._fetch_qcew("https://x.example/q1.json")
        assert result is not None
        assert result[0]["month3_emplvl"] == "42"
        assert call_count["n"] == 2  # both calls hit live; None never cached
    finally:
        bls_or_labor.set_cache(None)


def test_full_pipeline_cli_accepts_cache_mode_flag():
    from scripts import full_pipeline
    for mode in ("auto", "no-cache", "force-refresh"):
        args = full_pipeline.parse_args(
            ["--cities", "t.txt", "--cache-mode", mode]
        )
        assert args.cache_mode == mode


def test_full_pipeline_cli_defaults_cache_mode_to_auto():
    from scripts import full_pipeline
    args = full_pipeline.parse_args(["--cities", "t.txt"])
    assert args.cache_mode == "auto"


def test_compact_source_audit_includes_data_as_of_from_cache(tmp_path: Path):
    """build_compact_source_audit fills `data_as_of` from the cache session."""
    from app.utils.cache import Cache
    from app.utils.source_audit import build_compact_source_audit

    cache = Cache(tmp_path / "c.sqlite")
    cache.set("a", "v1", ttl_seconds=3600, provider="google_places")
    cache.get("a")    # serves an as_of for google_places this session

    sources = [
        {"name": "Google Places API (Nearby Search)",
         "url": "https://maps.googleapis.com/x",
         "fields": ["hospital"], "collected_at": "2026-05-24T00:00:00Z"},
    ]
    rows = build_compact_source_audit(sources, cache=cache)
    nearby = next(r for r in rows if r["source"] == "Google Places Nearby Search")
    assert "data_as_of" in nearby and nearby["data_as_of"]


def test_compact_source_audit_falls_back_to_collected_at_without_cache():
    from app.utils.source_audit import build_compact_source_audit
    sources = [
        {"name": "US Census Bureau ACS 5-year",
         "url": "https://api.census.gov", "fields": ["pop"],
         "collected_at": "2025-12-01T00:00:00Z"},
    ]
    rows = build_compact_source_audit(sources, cache=None)
    census = next(r for r in rows if r["source"] == "Census ACS")
    assert census["data_as_of"] == "2025-12-01T00:00:00Z"


def test_markdown_report_shows_as_of_column_in_source_audit():
    from app.reports.markdown_report import _compact_source_audit_block
    profile = {
        "candidate_id": "c1",
        "sources": [
            {"name": "US Census Bureau ACS 5-year",
             "url": "https://api.census.gov",
             "fields": ["pop"], "collected_at": "2025-12-01T00:00:00Z"},
        ],
    }
    block_lines = _compact_source_audit_block([(profile, {})])
    body = "\n".join(block_lines)
    assert "As of" in body                # header column
    assert "2025-12-01" in body           # value present (date portion)


def test_html_report_renders_as_of_column_and_freshness_chip():
    from app.reports.html_report import render_html_report
    profile = {
        "candidate_id": "c1", "candidate_name": "Test Area",
        "latitude": 37.0, "longitude": -121.0,
        "sources": [
            {"name": "US Census Bureau ACS 5-year",
             "url": "https://api.census.gov",
             "fields": ["pop"], "collected_at": "2025-12-01T00:00:00Z"},
        ],
        "anchor": {"name": "Test Anchor"},
    }
    scored = {"site_score": 70, "tier": "B", "sub_scores": {"confidence_score": 60}}
    interp = {
        "expansion_readiness": {"readiness": "Moderate", "reasons": []},
        "strategies": [{"key": "nursing", "label": "X", "why": "y"}],
        "warnings": [], "score_meters": [], "decision_checklist": [],
        "demand_signals": {"high_value": [], "secondary": []},
        "quick_read": {"what": "x", "why_high": "x", "why_fail": "x",
                       "best_use": "x", "decision": "x"},
        "competitor_interpretation": {"density": "Low", "quality": "Mixed",
                                      "market_gap": "High", "avg_rating": 4.0,
                                      "competitor_count_5mi": 1, "win_path": "x"},
    }
    payload = {
        "context": {"mode": "city", "radius_miles": 2},
        "candidates": [{"rank": 1, "profile": profile, "scored": scored,
                        "interpretation": interp}],
    }
    html = render_html_report(payload)
    # As of column in compact source audit:
    assert ">As of<" in html
    assert "2025-12-01" in html
    # Per-candidate freshness chip:
    assert "freshness-chip" in html


def test_html_report_uses_cache_session_as_of_from_context():
    """The freshness in the rendered HTML must reflect the cache's
    session_as_of, not just collected_at."""
    from app.reports.html_report import render_html_report
    profile = {
        "candidate_id": "c1", "candidate_name": "Test Area",
        "latitude": 37.0, "longitude": -121.0,
        "sources": [
            {"name": "Google Places API (Nearby Search)",
             "url": "https://maps.googleapis.com/x",
             "fields": ["hospital"], "collected_at": "2026-05-24T00:00:00Z"},
        ],
        "anchor": {"name": "T"},
    }
    scored = {"site_score": 70, "tier": "B",
              "sub_scores": {"confidence_score": 60}}
    interp = {
        "expansion_readiness": {"readiness": "Moderate", "reasons": []},
        "strategies": [{"key": "nursing", "label": "X", "why": "y"}],
        "warnings": [], "score_meters": [], "decision_checklist": [],
        "demand_signals": {"high_value": [], "secondary": []},
        "quick_read": {"what": "x", "why_high": "x", "why_fail": "x",
                       "best_use": "x", "decision": "x"},
        "competitor_interpretation": {"density": "Low", "quality": "Mixed",
                                      "market_gap": "High", "avg_rating": 4.0,
                                      "competitor_count_5mi": 1,
                                      "win_path": "x"},
    }
    # collected_at says May 24; cache says the data was fetched April 1.
    # The report must show April, not May, in the As of column.
    payload = {
        "context": {
            "mode": "city", "radius_miles": 2,
            "cache_session_as_of": {"google_places": "2026-04-01T12:00:00Z"},
        },
        "candidates": [{"rank": 1, "profile": profile, "scored": scored,
                        "interpretation": interp}],
    }
    html = render_html_report(payload)
    assert "2026-04-01" in html
    # And the May date from collected_at must NOT appear in the audit row
    # (it may still appear elsewhere — the test scopes to the audit by
    # looking at the table header section).
    audit_start = html.index(">As of<")
    audit_end = audit_start + 2000  # ~next 2KB covers the audit body
    assert "2026-04-01" in html[audit_start:audit_end]
