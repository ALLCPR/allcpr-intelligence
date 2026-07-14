"""
Interactive map section for the HTML site-intelligence dashboard.

Builds a self-contained map section (Leaflet inlined, OpenStreetMap tiles)
with tier-colored pins, filters, a ranked sidebar, map/card sync and
search-radius overlays. Pure rendering over data already in the scored
JSON — no pipeline run, no API calls.
"""
from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.reports.interpretation import STRATEGY_KEYS, build_candidate_interpretation

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _load_asset(name: str) -> str:
    """Return the text of a vendored asset under app/reports/assets/."""
    return (_ASSETS_DIR / name).read_text(encoding="utf-8")


def _num(value: Any) -> Optional[float]:
    # bool is a subclass of int; exclude it so a stray `true` is not a coordinate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _candidate_interpretation(item: Dict[str, Any]) -> Dict[str, Any]:
    """Use the precomputed interpretation, or derive it if absent."""
    interp = item.get("interpretation")
    if interp:
        return interp
    return build_candidate_interpretation(
        item.get("profile") or {}, item.get("scored") or {}
    )


def _pin_data(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Extract one pin record per mappable candidate.

    Returns (pins, skipped_count) where skipped_count is the number of
    candidates dropped because they had no usable coordinates.
    """
    pins: List[Dict[str, Any]] = []
    skipped = 0
    for index, item in enumerate(candidates, start=1):
        profile = item.get("profile") or {}
        lat = _num(profile.get("latitude"))
        lon = _num(profile.get("longitude"))
        if lat is None or lon is None:
            skipped += 1
            continue
        scored = item.get("scored") or {}
        interp = _candidate_interpretation(item)
        rank = item.get("rank") or profile.get("candidate_rank") or index
        anchor = profile.get("anchor") or {}
        if (profile.get("viability") or {}).get("needs_validation"):
            name = (profile.get("candidate_name")
                    or "Needs commercial site validation")
        else:
            name = (anchor.get("name") or profile.get("candidate_name")
                    or profile.get("candidate_id") or f"Area {rank}")
        pins.append({
            "rank": rank,
            "name": name,
            "lat": lat,
            "lon": lon,
            # Pins rank/display by area_score (always present); the gated
            # site_score is shown separately in the card.
            "site_score": _num(scored.get("area_score")) or _num(scored.get("site_score")) or 0.0,
            "area_score": _num(scored.get("area_score")) or 0.0,
            "candidate_type": str(scored.get("candidate_type") or ""),
            "tier": str(scored.get("tier") or "F"),
            "readiness": str(
                (interp.get("expansion_readiness") or {}).get("readiness", "Weak")
            ),
            "strategies": [
                s.get("key") for s in (interp.get("strategies") or [])
                if s.get("key")
            ],
            "card_id": f"candidate-{rank}",
        })
    return pins, skipped


# Tier colors — must match the badge palette in html_report.py and map.js.
_TIER_COLORS = {
    "A": "#15803d", "B": "#0f766e", "C": "#b45309",
    "D": "#c2410c", "F": "#b91c1c",
}

_TIERS = ("A", "B", "C", "D", "F")
_READINESS = ("Strong", "Moderate", "Weak")


def _checkbox(group: str, value: str) -> str:
    safe = _html.escape(value, quote=True)
    return (
        f'<label><input type="checkbox" data-filter="{group}" '
        f'value="{safe}" checked> {safe}</label>'
    )


def _build_filter_bar(total: int) -> str:
    """Build the tier / readiness / strategy filter toolbar."""
    total = int(total)
    tier = "".join(_checkbox("tier", t) for t in _TIERS)
    ready = "".join(_checkbox("readiness", r) for r in _READINESS)
    strategy = "".join(
        _checkbox("strategy", k) for k in sorted(STRATEGY_KEYS.values())
    )
    return (
        '<div class="map-toolbar" id="allcpr-map-toolbar">'
        '<button type="button" class="map-toolbar-toggle" '
        'id="allcpr-map-toggle">Filters &#9656;</button>'
        f'<fieldset><legend>Tier</legend>{tier}</fieldset>'
        f'<fieldset><legend>Readiness</legend>{ready}</fieldset>'
        f'<fieldset><legend>Strategy</legend>{strategy}</fieldset>'
        '<span class="map-count">Showing '
        f'<span id="allcpr-map-count">{total}</span> of '
        f'<span id="allcpr-map-total">{total}</span> candidates</span>'
        '<button type="button" class="map-reset" '
        'id="allcpr-map-reset">Reset</button>'
        '</div>'
    )


def _build_sidebar(pins: List[Dict[str, Any]]) -> str:
    """Build the ranked sidebar list, sorted by site score descending."""
    ordered = sorted(pins, key=lambda p: p["site_score"], reverse=True)
    rows = []
    for pin in ordered:
        color = _TIER_COLORS.get(pin["tier"], "#5f6c72")
        rows.append(
            f'<div class="map-row" data-rank="{_html.escape(str(pin["rank"]))}">'
            f'<span class="map-row-rank">#{_html.escape(str(pin["rank"]))}</span>'
            f'<span class="map-row-name">{_html.escape(str(pin["name"]))}</span>'
            f'<span class="map-chip" style="background:{color}">'
            f'{_html.escape(pin["tier"])}</span>'
            f'<span class="map-row-score">{pin["site_score"]:.1f}</span>'
            '</div>'
        )
    empty = ('<div class="map-empty" id="allcpr-map-empty" '
             'style="display:none">No areas match the current filters.</div>')
    return f'<aside class="map-sidebar">{"".join(rows)}{empty}</aside>'


def _pins_json(pins: List[Dict[str, Any]]) -> str:
    """Serialize pins for embedding in a <script> tag, safely escaped."""
    blob = json.dumps(pins, separators=(",", ":"))
    # Prevent the JSON from breaking out of the surrounding <script> element.
    return blob.replace("</", "<\\/")


def render_map_section(candidates: List[Dict[str, Any]],
                       context: Dict[str, Any]) -> str:
    """Render the interactive map section as a self-contained HTML fragment.

    Returns an empty string when no candidate has usable coordinates, so the
    caller can omit the section entirely.
    """
    pins, skipped = _pin_data(candidates)
    if not pins:
        return ""

    radius_miles = _num((context or {}).get("radius_miles")) or 0.0
    leaflet_css = _load_asset("leaflet.css")
    leaflet_js = _load_asset("leaflet.js")
    map_css = _load_asset("map.css")
    map_js = _load_asset("map.js")

    note = ""
    if skipped:
        plural = "area" if skipped == 1 else "areas"
        note = (f'<p class="map-note">{skipped} {plural} had no coordinates '
                f'and {"is" if skipped == 1 else "are"} not mapped.</p>')

    return (
        '<section class="map-section" id="allcpr-map-section">'
        f'<style>{leaflet_css}{map_css}</style>'
        '<h2>Candidate map</h2>'
        f'{_build_filter_bar(len(pins))}'
        '<div class="map-layout">'
        '<div id="allcpr-map"></div>'
        f'{_build_sidebar(pins)}'
        '</div>'
        f'{note}'
        f'<script>{leaflet_js}</script>'
        '<script>window.__ALLCPR_PINS__=' + _pins_json(pins) + ';'
        f'window.__ALLCPR_RADIUS_MILES__={radius_miles};'
        f'window.__ALLCPR_TIER_COLORS__={json.dumps(_TIER_COLORS)};</script>'
        f'<script>{map_js}</script>'
        '</section>'
    )
