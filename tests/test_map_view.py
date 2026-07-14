"""Tests for the interactive map section."""
from __future__ import annotations

import os

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

from app.reports import map_view


def _sample_candidates():
    """Two mappable candidates plus one with no coordinates."""
    def mk(rank, name, lat, lon, score, tier, readiness, strat):
        return {
            "rank": rank,
            "profile": {
                "candidate_id": f"c{rank}",
                "candidate_name": name,
                "latitude": lat,
                "longitude": lon,
                "anchor": {"name": name},
            },
            "scored": {"site_score": score, "tier": tier},
            "interpretation": {
                "expansion_readiness": {"readiness": readiness},
                "strategies": [{"key": strat, "label": strat, "why": "x"}],
            },
        }
    no_coords = mk(3, "No Coords Area", None, None, 50.0, "C", "Weak", "partnership")
    no_coords["profile"]["latitude"] = None
    no_coords["profile"]["longitude"] = None
    return [
        mk(1, "Santana Row", 37.3213, -121.9478, 76.6, "B", "Moderate", "nursing"),
        mk(2, "Downtown San Jose", 37.3337, -121.8907, 73.5, "B", "Moderate", "hospital"),
        no_coords,
    ]


def test_load_asset_returns_leaflet_library():
    js = map_view._load_asset("leaflet.js")
    assert len(js) > 100000
    assert "leaflet" in js.lower()


def test_pin_data_extracts_mappable_candidates():
    pins, skipped = map_view._pin_data(_sample_candidates())
    assert skipped == 1                      # the no-coords candidate
    assert len(pins) == 2
    first = pins[0]
    assert first["rank"] == 1
    assert first["name"] == "Santana Row"
    assert first["lat"] == 37.3213 and first["lon"] == -121.9478
    assert first["tier"] == "B"
    assert first["readiness"] == "Moderate"
    assert first["strategies"] == ["nursing"]
    assert first["card_id"] == "candidate-1"
    assert first["site_score"] == 76.6


def test_pin_data_skips_non_numeric_coords():
    bad = [{
        "rank": 9,
        "profile": {"candidate_id": "c9", "latitude": "n/a", "longitude": None},
        "scored": {"site_score": 10, "tier": "F"},
        "interpretation": {"expansion_readiness": {"readiness": "Weak"},
                           "strategies": []},
    }]
    pins, skipped = map_view._pin_data(bad)
    assert pins == []
    assert skipped == 1


def test_build_filter_bar_has_all_groups():
    bar = map_view._build_filter_bar(total=5)
    for value in ("A", "B", "C", "D", "F"):
        assert f'data-filter="tier" value="{value}"' in bar
    for value in ("Strong", "Moderate", "Weak"):
        assert f'data-filter="readiness" value="{value}"' in bar
    for key in ("nursing", "hospital", "partnership"):
        assert f'data-filter="strategy" value="{key}"' in bar
    assert 'id="allcpr-map-count"' in bar
    assert ">5<" in bar                       # total injected
    assert 'id="allcpr-map-reset"' in bar
    assert 'id="allcpr-map-toggle"' in bar    # mobile collapse button


def test_build_sidebar_rows_sorted_by_score():
    pins, _ = map_view._pin_data(_sample_candidates())
    # Feed in reverse-score order to prove the builder sorts.
    sidebar = map_view._build_sidebar(list(reversed(pins)))
    assert sidebar.index("Santana Row") < sidebar.index("Downtown San Jose")
    assert 'data-rank="1"' in sidebar
    assert 'id="allcpr-map-empty"' in sidebar          # empty-state element
    assert "76.6" in sidebar


def test_build_sidebar_escapes_names():
    pins = [{
        "rank": 1, "name": "A & B <Plaza>", "lat": 1.0, "lon": 2.0,
        "site_score": 50.0, "tier": "C", "readiness": "Weak",
        "strategies": [], "card_id": "candidate-1",
    }]
    sidebar = map_view._build_sidebar(pins)
    assert "&amp;" in sidebar and "&lt;Plaza&gt;" in sidebar
    assert "<Plaza>" not in sidebar


def test_render_map_section_contains_core_elements():
    out = map_view.render_map_section(_sample_candidates(),
                                      {"radius_miles": 2})
    assert 'id="allcpr-map"' in out                 # map container
    assert "__ALLCPR_PINS__" in out                 # embedded data blob
    assert "__ALLCPR_TIER_COLORS__" in out          # injected tier palette
    assert "Santana Row" in out
    assert 'data-filter="tier"' in out              # filter controls
    assert "map-legend" in out                      # legend class (inlined map.js)
    assert "leaflet" in out.lower()                 # Leaflet inlined
    assert "L.circleMarker" in out                  # map.js inlined


def test_render_map_section_missing_coords_note():
    out = map_view.render_map_section(_sample_candidates(),
                                      {"radius_miles": 2})
    assert "1 area" in out and "not mapped" in out


def test_render_map_section_empty_returns_blank():
    assert map_view.render_map_section([], {"radius_miles": 2}) == ""


def test_render_map_section_no_key_leakage():
    cands = _sample_candidates()
    cands[0]["profile"]["website"] = "https://x.example/?key=SECRET"
    out = map_view.render_map_section(cands, {"radius_miles": 2})
    assert "SECRET" not in out


def test_render_map_section_single_candidate_runs():
    one = [_sample_candidates()[0]]
    out = map_view.render_map_section(one, {"radius_miles": 2})
    assert 'id="allcpr-map"' in out


def test_html_report_includes_map_section():
    from app.reports.html_report import render_html_report
    payload = {
        "context": {"mode": "metro_comparison", "radius_miles": 2},
        "candidates": _sample_candidates(),
    }
    html = render_html_report(payload)
    assert 'id="allcpr-map-section"' in html
    assert 'id="allcpr-map"' in html
    assert "__ALLCPR_PINS__" in html
    # The map sits above the candidate cards.
    assert html.index("allcpr-map-section") < html.index('class="candidate-card"')
