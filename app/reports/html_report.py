"""
Dashboard-style HTML report rendering from scored_locations.json.

The HTML report is an executive decision dashboard: a sticky summary with the
executive verdict, candidate cards with colored tier / readiness badges, styled
score bars, a compact source audit and collapsible raw detail. It is mobile
readable and print friendly, with no external/paid dependencies.

Report styles (``report_style``): executive | detailed | debug. The raw detail
``<details>`` blocks render open in detailed/debug and collapsed in executive.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.reports.interpretation import (
    build_candidate_interpretation,
    build_report_interpretation,
)
from app.reports.map_view import render_map_section
from app.evaluation.center_recommendations import (
    build_center_recommendations_from_report,
)
from app.enrichers.course_classifier import HYBRID_COURSE_KEY, HYBRID_COURSE_NOTE
from app.enrichers.anchor_status import (
    AREA_PROXY,
    AREA_PROXY_CHECKLIST,
    INVALID_ANCHOR,
    area_display_name,
    assess_anchor,
)
from app.utils.logging_utils import get_logger
from app.utils.report_safety import strip_sensitive_query_params
from app.utils.source_audit import (
    _FAMILY_TO_PROVIDER,
    _source_family,
    build_compact_source_audit_for_candidates,
    build_source_audit_rows,
)

logger = get_logger(__name__)

VALID_STYLES = ("executive", "detailed", "debug")

_TIER_CLASS = {"A": "tier-a", "B": "tier-b", "C": "tier-c", "D": "tier-d",
               "F": "tier-f"}
_READINESS_CLASS = {"Strong": "rd-strong", "Moderate": "rd-moderate",
                    "Weak": "rd-weak"}


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #

def _esc(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    return html.escape(str(value), quote=True)


def _score(value: Any) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "unknown"


def _money(value: Any) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "unknown"


def _safe_url(value: Any) -> str:
    return strip_sensitive_query_params(str(value or ""))


def _link(url: Any, label: str) -> str:
    safe = _safe_url(url)
    if not safe:
        return "<span class=\"muted\">unknown</span>"
    return f"<a href=\"{html.escape(safe, quote=True)}\">{_esc(label)}</a>"


def _list(items: Iterable[Any], empty: str = "unknown") -> str:
    rows = [f"<li>{_esc(item)}</li>" for item in items if item not in (None, "")]
    if not rows:
        return f"<p class=\"muted\">{_esc(empty)}</p>"
    return "<ul>" + "".join(rows) + "</ul>"


def _score_bar(label: str, value: Any) -> str:
    """Render one styled horizontal score bar (0..100)."""
    if isinstance(value, (int, float)):
        pct = max(0.0, min(100.0, float(value)))
        if pct >= 70:
            cls = "bar-good"
        elif pct >= 40:
            cls = "bar-mid"
        else:
            cls = "bar-low"
        vtext = f"{pct:.0f}"
    else:
        pct = 0.0
        cls = "bar-low"
        vtext = "n/a"
    return (
        "<div class=\"meter\">"
        f"<span class=\"meter-label\">{_esc(label)}</span>"
        "<span class=\"meter-track\">"
        f"<span class=\"meter-fill {cls}\" style=\"width:{pct:.0f}%\"></span>"
        "</span>"
        f"<span class=\"meter-val\">{vtext}</span>"
        "</div>"
    )


# --------------------------------------------------------------------------- #
# Detail blocks (rendered inside collapsible <details>)
# --------------------------------------------------------------------------- #

def _top_competitors(profile: Dict[str, Any], max_rows: int = 8) -> str:
    competitors = profile.get("competitors_sample") or profile.get("competitors") or []
    if not competitors:
        return "<p class=\"muted\">No competitors found within radius.</p>"
    competitors = sorted(
        competitors,
        key=lambda c: c.get("distance_miles")
        if c.get("distance_miles") is not None else 9999.0,
    )[:max_rows]
    rows = []
    for comp in competitors:
        web = comp.get("website_analysis") or {}
        signals = []
        if web.get("checked"):
            if web.get("detected"):
                signals.append("detected: " + ", ".join(web.get("detected")[:3]))
            if web.get("missing"):
                signals.append("missing: " + ", ".join(web.get("missing")[:3]))
        elif web:
            signals.append("unknown")
        rows.append(
            "<tr>"
            f"<td>{_esc(comp.get('name'))}</td>"
            f"<td>{_esc(comp.get('formatted_address'))}</td>"
            f"<td>{_score(comp.get('distance_miles'))} mi</td>"
            f"<td>{_esc(comp.get('rating'))}</td>"
            f"<td>{_link(comp.get('website'), 'website')}</td>"
            f"<td>{_esc('; '.join(signals) or 'not checked')}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Name</th><th>Address</th><th>Distance</th>"
        "<th>Rating</th><th>Website</th><th>Web signals</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _economic_profile(profile: Dict[str, Any]) -> str:
    census = (profile.get("economy") or {}).get("census") or {}
    values = census.get("values") or {}
    indicators = census.get("indicators") or {}
    pop = values.get("population")
    bits = [
        f"Geography: {_esc(census.get('geo_desc') or 'unresolved')}",
        f"Population: {_esc(f'{pop:,.0f}' if isinstance(pop, (int, float)) else 'unknown')}",
        f"Median household income: {_money(values.get('median_household_income'))}",
        f"Median age: {_esc(values.get('median_age'))}",
    ]
    for key in ("healthcare_employment_share", "bachelors_or_higher_share",
                "working_age_share", "employment_rate"):
        val = indicators.get(key)
        bits.append(
            f"{key}: {val:.1%}" if isinstance(val, (int, float))
            else f"{key}: unknown"
        )
    return _list(bits)


def _photo_meta(anchor: Dict[str, Any]) -> str:
    photos = anchor.get("photos") or []
    if not photos:
        return "<p class=\"muted\">Anchor photo metadata: unavailable</p>"
    first = photos[0]
    bits = [
        f"photo_reference: {first.get('photo_reference') or 'unknown'}",
        f"width: {first.get('width') or 'unknown'}",
        f"height: {first.get('height') or 'unknown'}",
    ]
    attrs = first.get("attributions") or []
    if attrs:
        bits.append("attributions: " + " ".join(str(a) for a in attrs))
    return "<p class=\"photo-meta\">" + _esc("; ".join(bits)) + "</p>"


def _accessibility(profile: Dict[str, Any]) -> str:
    signals = (profile.get("accessibility") or {}).get("signals") or {}
    if not signals:
        return "<p class=\"muted\">Accessibility signals unknown.</p>"
    rows = []
    for key, sig in signals.items():
        if not isinstance(sig, dict):
            continue
        dist = sig.get("distance_miles")
        dist_text = f"{dist} mi" if isinstance(dist, (int, float)) else "unknown"
        rows.append(
            "<tr>"
            f"<td>{_esc(key)}</td>"
            f"<td>{_esc(sig.get('status'))}</td>"
            f"<td>{_esc(sig.get('nearest_name') or sig.get('nearby_places_1mi'))}</td>"
            f"<td>{_esc(dist_text)}</td>"
            f"<td>{_esc(sig.get('notes'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Signal</th><th>Status</th><th>Nearest/count</th>"
        "<th>Distance</th><th>Notes</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _job_demand(profile: Dict[str, Any], scored: Dict[str, Any]) -> str:
    job = scored.get("job_demand") or {}
    job_block = profile.get("job_demand") or {}
    top = job.get("top_employers") or []
    samples = job_block.get("sample_postings") or []
    bits = [
        f"job_certification_demand_score: "
        f"{_score(job.get('job_certification_demand_score'))}",
        f"confidence: {_esc(job.get('job_demand_data_confidence'))}",
        f"active_postings_count: {_esc(job.get('active_postings_count'))}",
        f"certification_postings_count: {_esc(job.get('certification_postings_count'))}",
    ]
    if top:
        bits.append("top_employers: " + "; ".join(
            f"{_esc(e.get('employer'))} ({_esc(e.get('posting_count'))})"
            for e in top[:5]
        ))
    html_bits = _list(bits)
    if not samples:
        return html_bits + "<p class=\"muted\">No cited job-posting rows supplied.</p>"
    rows = []
    for posting in samples[:5]:
        certs = ", ".join(posting.get("certification_signals") or []) or "none"
        roles = ", ".join(posting.get("role_signals") or []) or "none"
        rows.append(
            "<tr>"
            f"<td>{_esc(posting.get('employer'))}</td>"
            f"<td>{_esc(posting.get('title'))}</td>"
            f"<td>{_esc(certs)}</td>"
            f"<td>{_esc(roles)}</td>"
            f"<td>{_link(posting.get('source_url'), 'source')}</td>"
            "</tr>"
        )
    return html_bits + (
        "<table><thead><tr><th>Employer</th><th>Title</th><th>Cert signals</th>"
        "<th>Role signals</th><th>Source</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# --------------------------------------------------------------------------- #
# Interpretation-driven blocks
# --------------------------------------------------------------------------- #

def _demand_signal_table(rows: List[Dict[str, Any]]) -> str:
    body = "".join(
        "<tr>"
        f"<td>{_esc(r['signal'])}</td>"
        f"<td>{_esc(r['count'])}</td>"
        f"<td>{_esc(r['importance'])}</td>"
        f"<td>{_esc(r['why'])}</td>"
        "</tr>"
        for r in rows
    )
    return (
        "<table><thead><tr><th>Signal</th><th>Count</th>"
        "<th>Business importance</th><th>Why it matters</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _freshness_chip(profile: Dict[str, Any],
                    session_as_of: Optional[Dict[str, str]] = None) -> str:
    """Render a colored chip showing how old this candidate's data is.

    When ``session_as_of`` is supplied, each source's freshness is taken
    from the cache snapshot for that source's provider (falling back to
    ``collected_at`` when the provider isn't represented in the snapshot).
    """
    sources = profile.get("sources") or []
    timestamps: List[str] = []
    for source in sources:
        name = str(source.get("name") or "")
        url = str(source.get("url") or "")
        ts = ""
        if session_as_of:
            family, _ = _source_family(name, url)
            provider = _FAMILY_TO_PROVIDER.get(family)
            if provider:
                ts = session_as_of.get(provider) or ""
        if not ts:
            ts = str(source.get("collected_at") or "")
        if ts:
            timestamps.append(ts)
    if not timestamps:
        return ""
    try:
        from datetime import datetime, timezone
        parsed = []
        for ts in timestamps:
            t = ts.replace("Z", "+00:00")
            parsed.append(datetime.fromisoformat(t).astimezone(timezone.utc))
        oldest = min(parsed)
        age_days = (datetime.now(timezone.utc) - oldest).days
    except (ValueError, TypeError):
        return ""
    if age_days < 30:
        cls = "rd-strong"
    elif age_days < 180:
        cls = "rd-moderate"
    else:
        cls = "rd-weak"
    label = (
        f"data freshness: oldest {age_days} day{'s' if age_days != 1 else ''} old "
        f"({oldest.strftime('%Y-%m-%d')})"
    )
    return f'<span class="freshness-chip badge {cls}">{_esc(label)}</span>'


def _quick_read(interp: Dict[str, Any]) -> str:
    qr = interp.get("quick_read") or {}
    return (
        "<div class=\"quick-read\">"
        f"<p><strong>What this location is:</strong> {_esc(qr.get('what'))}</p>"
        f"<p><strong>Why it scores high:</strong> {_esc(qr.get('why_high'))}</p>"
        f"<p><strong>Why it may fail:</strong> {_esc(qr.get('why_fail'))}</p>"
        f"<p><strong>Best use case:</strong> {_esc(qr.get('best_use'))}</p>"
        f"<p><strong>Decision:</strong> {_esc(qr.get('decision'))}</p>"
        "</div>"
    )


_CTYPE_BADGE = {
    "verified_commercial_listing": ("Verified site", "ct-verified"),
    "commercial_area_proxy": ("Commercial area", "ct-area"),
    "landmark_proxy": ("Area-level (proxy)", "ct-landmark"),
    "invalid_or_low_confidence": ("Low confidence", "ct-invalid"),
}


def _candidate_type_badge(scored: Dict[str, Any]) -> str:
    ct = str(scored.get("candidate_type") or "landmark_proxy")
    label, cls = _CTYPE_BADGE.get(ct, (ct, "ct-landmark"))
    return f'<span class="badge {cls}">{_esc(label)}</span>'


def _chip(label: str, state: Any) -> str:
    if isinstance(state, bool):
        cls = "chip-yes" if state else "chip-no"
        val = "Yes" if state else "No"
    else:
        s = str(state or "proxy").lower()
        cls = {"confirmed": "chip-yes", "tested": "chip-warn"}.get(s, "chip-no")
        val = {"proxy": "Proxy only", "tested": "Tested",
               "confirmed": "Confirmed"}.get(s, s.capitalize())
    return f'<span class="chip {cls}">{_esc(label)}: {_esc(val)}</span>'


def _validation_chips(scored: Dict[str, Any]) -> str:
    vf = scored.get("validation_flags") or {}
    chips = [
        _chip("Lease-ready", bool(vf.get("lease_ready"))),
        _chip("Commercial listing", bool(vf.get("commercial_listing_validated"))),
        _chip("Parking", bool(vf.get("parking_validated"))),
        _chip("Rent", bool(vf.get("rent_validated"))),
        _chip("Demand", vf.get("demand_validated") or "proxy"),
    ]
    return '<p class="chips">' + "".join(chips) + "</p>"


def _site_score_display(scored: Dict[str, Any]) -> str:
    ss = scored.get("site_score")
    if isinstance(ss, (int, float)):
        return _score(ss)
    return '<span class="not-validated">Not validated</span>'


def _feasibility_block(scored: Dict[str, Any]) -> str:
    bf = scored.get("business_feasibility") or {}

    def m(v: Any) -> str:
        return _score(v) if isinstance(v, (int, float)) else "<span class=\"muted\">n/a</span>"

    rev = bf.get("monthly_revenue_range") or [None, None]
    fixed = bf.get("monthly_fixed_cost_range") or [None, None]
    be = bf.get("breakeven_students_per_month")
    risk = bf.get("risk_level") or "—"
    return (
        "<div class=\"interp-grid\">"
        f"<div><span>Rent</span><strong>{m(bf.get('rent_score'))}</strong></div>"
        f"<div><span>Parking</span><strong>{m(bf.get('parking_score'))}</strong></div>"
        f"<div><span>Classroom fit</span><strong>{m(bf.get('classroom_fit_score'))}</strong></div>"
        f"<div><span>Access</span><strong>{m(bf.get('access_score'))}</strong></div>"
        f"<div><span>Visibility</span><strong>{m(bf.get('visibility_score'))}</strong></div>"
        f"<div><span>Lease readiness</span><strong>{m(bf.get('lease_readiness_score'))}</strong></div>"
        "</div>"
        "<table><tbody>"
        f"<tr><td>Break-even students / month</td><td>{_esc(be if be is not None else 'n/a')}</td></tr>"
        f"<tr><td>Est. monthly revenue</td><td>{_money(rev[0])} – {_money(rev[1])}</td></tr>"
        f"<tr><td>Est. monthly fixed cost</td><td>{_money(fixed[0])} – {_money(fixed[1])}</td></tr>"
        f"<tr><td>Risk level</td><td><strong>{_esc(risk)}</strong></td></tr>"
        "</tbody></table>"
        "<p class=\"muted\">Model-based estimates — confirm with a real "
        "listing.</p>"
    )


def _competition_bands(scored: Dict[str, Any]) -> str:
    cd = scored.get("competition_detail") or {}
    wp = cd.get("website_presence")
    wp_txt = f"{wp:.0%}" if isinstance(wp, (int, float)) else "unknown"
    avg = cd.get("avg_rating")
    avg_txt = f"{avg:.2f}" if isinstance(avg, (int, float)) else "unknown"
    return (
        "<div class=\"interp-grid\">"
        f"<div><span>0–1 mi</span><strong>{_esc(cd.get('band_0_1_mi'))}</strong></div>"
        f"<div><span>1–3 mi</span><strong>{_esc(cd.get('band_1_3_mi'))}</strong></div>"
        f"<div><span>3–5 mi</span><strong>{_esc(cd.get('band_3_5_mi'))}</strong></div>"
        f"<div><span>Direct CPR/BLS</span><strong>{_esc(cd.get('direct_competitors'))}</strong></div>"
        f"<div><span>General</span><strong>{_esc(cd.get('general_competitors'))}</strong></div>"
        f"<div><span>Pressure</span><strong>{_esc(cd.get('competition_pressure_band'))}</strong></div>"
        f"<div><span>Avg rating</span><strong>{_esc(avg_txt)}</strong></div>"
        f"<div><span>Have website</span><strong>{_esc(wp_txt)}</strong></div>"
        "</div>"
    )


def _historical_performance_html(profile: Dict[str, Any],
                                 scored: Dict[str, Any]) -> str:
    hist = profile.get("historical_performance") or {}
    if not hist:
        return "<p class=\"muted\">No Enrollware history loaded for this run.</p>"

    sub = scored.get("sub_scores") or {}
    score = sub.get("historical_performance_score")
    recent = hist.get("recent_activity") or {}
    latest = recent.get("latest_class_date") or "unknown"
    recent_count = recent.get("classes_last_180_days")
    recent_text = (
        f"{recent_count} class(es) in latest 180-day window"
        if isinstance(recent_count, int) else "unknown"
    )
    courses = hist.get("course_type_frequency") or []
    course_rows = "".join(
        f"<tr><td>{_esc(c.get('label'))}</td><td>{_esc(c.get('classes'))}</td></tr>"
        for c in courses[:6]
    )
    if not course_rows:
        course_rows = "<tr><td colspan=\"2\">unknown</td></tr>"
    reasons = _list(hist.get("reasons") or [], "No historical rationale.")
    return (
        "<div class=\"interp-grid\">"
        f"<div><span>Historical score</span><strong>{_score(score)}</strong></div>"
        f"<div><span>Confidence</span><strong>{_esc(hist.get('confidence'))}</strong></div>"
        f"<div><span>Held classes</span><strong>{_esc(hist.get('total_classes', 0))}</strong></div>"
        f"<div><span>Avg students</span><strong>{_score(hist.get('average_students_per_class'))}</strong></div>"
        f"<div><span>Fill rate</span><strong>{_score(hist.get('fill_rate_percent'))}</strong></div>"
        f"<div><span>Latest held class</span><strong>{_esc(latest)}</strong></div>"
        "</div>"
        "<p class=\"muted\">Historical ALLCPR performance uses completed held "
        "classes only. Future scheduled classes and zero-enrollment placeholder "
        "rows are excluded from enrollment averages.</p>"
        f"<p class=\"muted\">Matched {_esc(hist.get('area_label') or 'unknown')} "
        f"via {_esc(hist.get('match_type') or 'none')}; recent activity: "
        f"{_esc(recent_text)}.</p>"
        "<table><thead><tr><th>Course type</th><th>Held classes</th></tr></thead>"
        f"<tbody>{course_rows}</tbody></table>"
        f"{reasons}"
    )


def _zip_demand_html(profile: Dict[str, Any], scored: Dict[str, Any]) -> str:
    """ZIP-level course demand: score summary, 3-bucket table, CSS bar visual.

    The three charted channels are fixed (ARC CPR / ARC BLS / AHA BLS) — see
    the zip-demand spec; OTHER stays out of the charts by design.
    """
    zd = profile.get("zip_demand") or {}
    combined = zd.get("combined") or {}
    if not combined or not zd.get("resolved_zips"):
        return ("<p class=\"muted\">No ZIP-level class history matched this "
                "candidate (no Locations export, no resolvable ZIP, or no "
                "held classes in range).</p>")

    # Provenance caption: which ZIP(s), how matched.
    zips = zd.get("resolved_zips") or []
    basis = str(zd.get("match_basis") or "none")
    basis_text = {
        "exact_plus_radius": (
            f"ZIP {_esc(zd.get('primary_zip'))} + "
            f"{max(0, len(zips) - 1)} nearby "
            f"(within {_esc(zd.get('radius_miles'))} mi)"),
        "exact": f"ZIP {_esc(zd.get('primary_zip'))} (exact match)",
        "city": (
            "city-level fallback ("
            f"{', '.join(_esc(z) for z in zips[:4])}"
            f"{'…' if len(zips) > 4 else ''})"),
    }.get(basis, "unmatched")

    # Score summary line: Base -> Adjustment -> Final + Confidence + Strength.
    base = scored.get("base_score", scored.get("area_score"))
    _raw_adj = scored.get("zip_demand_adjustment", zd.get("adjustment"))
    try:
        adj = float(_raw_adj) if _raw_adj is not None else 0.0
    except (TypeError, ValueError):
        adj = 0.0   # renderers never crash on a malformed legacy payload
    final = scored.get("final_score")
    if final is None and isinstance(base, (int, float)):
        final = round(max(0.0, min(100.0, float(base) + adj)), 2)
    conf_adj = scored.get("confidence_score_adjusted")
    strength = zd.get("strength") or "unknown"
    summary = (
        "<div class=\"interp-grid\">"
        f"<div><span>Base score</span><strong>{_score(base)}</strong></div>"
        f"<div><span>ZIP demand score</span><strong>{_score(zd.get('zip_demand_score'))}</strong></div>"
        f"<div><span>Adjustment</span><strong>{adj:+.1f}</strong></div>"
        f"<div><span>Final score</span><strong>{_score(final)}</strong></div>"
        f"<div><span>Confidence (adj.)</span><strong>{_score(conf_adj)}</strong></div>"
        f"<div><span>Demand strength</span><strong>{_esc(strength)}</strong></div>"
        "</div>"
    )

    # 3-bucket table + bar visual from the combined profile.
    labels = zd.get("bucket_labels") or {}
    buckets = combined.get("buckets") or {}
    order = ("ARC_CPR", "ARC_BLS", "AHA_BLS")
    max_classes = max(
        [int((buckets.get(b) or {}).get("classes") or 0) for b in order] + [1]
    )
    rows, bars = [], []
    fill = combined.get("fill_rate")
    overall_fill_text = (f"{fill:.1f}%"
                         if isinstance(fill, (int, float)) else "unknown")
    for b in order:
        stat = buckets.get(b) or {}
        classes = int(stat.get("classes") or 0)
        label = labels.get(b, b)
        avg = stat.get("avg_students")
        b_fill = stat.get("fill_rate")
        # Per-channel fill; "—" when that channel has no capacity-known rows.
        b_fill_text = (f"{b_fill:.1f}%"
                       if isinstance(b_fill, (int, float)) else "—")
        rows.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td>{classes}</td>"
            f"<td>{_esc(stat.get('students') if stat.get('students') is not None else 'unknown')}</td>"
            f"<td>{_score(avg)}</td>"
            f"<td>{_esc(b_fill_text)}</td></tr>"
        )
        pct = round(100.0 * classes / max_classes)
        bars.append(
            "<div class=\"zip-bar\">"
            f"<span class=\"zip-bar-label\">{_esc(label)}</span>"
            "<span class=\"zip-bar-track\">"
            f"<span class=\"zip-bar-fill\" style=\"width:{pct}%\"></span>"
            "</span>"
            f"<span class=\"zip-bar-value\">{classes} classes"
            f"{f' · {avg} avg' if isinstance(avg, (int, float)) else ''}</span>"
            "</div>"
        )

    return (
        summary
        + f"<p class=\"muted\">Demand basis: {basis_text}; held classes only; "
          f"overall fill {_esc(overall_fill_text)}; "
          f"latest class {_esc(combined.get('latest_class_date') or 'unknown')}."
          "</p>"
        + "<table><thead><tr><th>Course type</th><th>Classes</th>"
          "<th>Students</th><th>Avg students/class</th><th>Fill rate</th>"
          "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        + "<div class=\"zip-bars\">" + "".join(bars) + "</div>"
    )


def _next_actions_html(scored: Dict[str, Any]) -> str:
    acts = scored.get("next_actions") or []
    if not acts:
        return ""
    return "<ul class=\"checklist\">" + "".join(
        f"<li>{_esc(a)}</li>" for a in acts
    ) + "</ul>"


# An "unknown"/"n/a" value, optionally wrapped in the muted span, optionally a
# blank money range ("unknown – unknown").
_UNKNOWN_VAL = (
    r"(?:<span class=\"muted\">)?(?:unknown|n/a|—)(?:</span>)?"
    r"(?:\s*–\s*(?:<span class=\"muted\">)?(?:unknown|n/a)(?:</span>)?)?"
)
_UNKNOWN_CELL_RES = (
    # interp-grid cell: <div><span>Label</span><strong>unknown</strong></div>
    re.compile(r"<div><span>[^<]*</span><strong>" + _UNKNOWN_VAL
               + r"</strong></div>", re.I),
    # table row (any column count) where every value cell is unknown/n-a
    re.compile(r"<tr><td>[^<]*</td>(?:<td>(?:<strong>)?" + _UNKNOWN_VAL
               + r"(?:</strong>)?</td>)+</tr>", re.I),
)


def _strip_unknown_cells(card_html: str) -> str:
    """Remove grid cells / table rows whose value is unknown or n/a.

    Used in the executive style only: an empty field is noise there, while the
    detailed/debug styles keep every field visible (including the explicit
    unknowns) for auditability.
    """
    for rx in _UNKNOWN_CELL_RES:
        card_html = rx.sub("", card_html)
    return card_html


def _candidate_card(item: Dict[str, Any], report_style: str,
                    fallback_rank: int = 0,
                    session_as_of: Optional[Dict[str, str]] = None) -> str:
    profile = item.get("profile") or {}
    scored = item.get("scored") or {}
    interp = item.get("interpretation") or build_candidate_interpretation(
        profile, scored)
    anchor = profile.get("anchor") or {}
    # Rank also forms the card's DOM id ("candidate-{rank}"); the map's
    # "View full card" links target it. The fallback chain must match
    # map_view._pin_data so the two never disagree.
    rank = item.get("rank") or profile.get("candidate_rank") or fallback_rank
    tier = str(scored.get("tier") or "F")
    readiness = (interp.get("expansion_readiness") or {}).get("readiness", "Weak")

    # Anchor status: lead the card with the AREA, never a random POI. The anchor
    # is shown separately as a labeled proxy so a landmark never reads as a site.
    aa = assess_anchor(profile)
    area_name = profile.get("area_display_name") or area_display_name(profile)
    anchor_name = aa.anchor_display_name or anchor.get("name") or ""
    lease_validated = bool((scored.get("validation_flags") or {}).get("lease_ready"))
    lease_text = ("Validated" if lease_validated and not aa.site_score_withheld
                  else "Not validated")

    # Score bars. Executive view drops bars with no value at all (n/a);
    # detailed/debug keep them so the gap itself stays visible.
    meters = "".join(
        _score_bar(m["label"], m["value"])
        for m in (interp.get("score_meters") or [])
        if report_style != "executive" or isinstance(m.get("value"), (int, float))
    )

    # Demand signals.
    ds = interp.get("demand_signals") or {}
    high_value = ds.get("high_value") or []
    secondary = ds.get("secondary") or []
    if high_value:
        demand_html = _demand_signal_table(high_value)
    else:
        demand_html = "<p class=\"muted\">No high-value demand signals nearby.</p>"
    if secondary:
        demand_html += (
            f"<details><summary>Secondary demand signals ({len(secondary)})"
            f"</summary>{_demand_signal_table(secondary)}</details>"
        )

    # Strategies.
    strategies = interp.get("strategies") or []
    strat_html = "<ol>" + "".join(
        f"<li><strong>{_esc(s['label'])}</strong> — {_esc(s['why'])}</li>"
        for s in strategies
    ) + "</ol>" if strategies else "<p class=\"muted\">No strategy match.</p>"

    # Competitor interpretation.
    ci = interp.get("competitor_interpretation") or {}
    avg = ci.get("avg_rating")
    avg_text = f"{avg:.2f}" if isinstance(avg, (int, float)) else "unknown"
    comp_html = (
        "<div class=\"interp-grid\">"
        f"<div><span>Density</span><strong>{_esc(ci.get('density'))}</strong></div>"
        f"<div><span>Quality</span><strong>{_esc(ci.get('quality'))}</strong></div>"
        f"<div><span>Market gap</span><strong>{_esc(ci.get('market_gap'))}</strong></div>"
        f"<div><span>Avg rating</span><strong>{_esc(avg_text)}</strong></div>"
        "</div>"
        f"<p>{_esc(ci.get('win_path'))}</p>"
    )

    # Readiness reasons.
    readiness_reasons = _list(
        (interp.get("expansion_readiness") or {}).get("reasons") or [],
        "no reasons recorded",
    )

    # Warnings & checklist.
    warnings_html = _list(interp.get("warnings") or [],
                          "No warnings flagged.")
    checklist_html = "<ul class=\"checklist\">" + "".join(
        f"<li>{_esc(c)}</li>" for c in (interp.get("decision_checklist") or [])
    ) + "</ul>"

    # Profitability.
    prof = scored.get("profitability_estimate") or {}
    prof_html = (
        "<table><thead><tr><th>Scenario</th><th>Est. monthly students</th>"
        "<th>Est. monthly revenue</th></tr></thead><tbody>"
        f"<tr><td>Low</td><td>{_esc(prof.get('students_low'))}</td>"
        f"<td>{_money(prof.get('revenue_low'))}</td></tr>"
        f"<tr><td>Mid</td><td>{_esc(prof.get('students_mid'))}</td>"
        f"<td>{_money(prof.get('revenue_mid'))}</td></tr>"
        f"<tr><td>High</td><td>{_esc(prof.get('students_high'))}</td>"
        f"<td>{_money(prof.get('revenue_high'))}</td></tr>"
        "</tbody></table>"
        "<p class=\"muted\">Revenue and student bands are model-based "
        "estimates, not measured figures.</p>"
    )

    # Raw detail (collapsible).
    detail_open = " open" if report_style in ("detailed", "debug") else ""
    rent = scored.get("rent") or {}
    raw_detail = (
        f"<details{detail_open}><summary>Full data &amp; sources</summary>"
        "<section><h4>Anchor</h4>"
        f"<p>{_esc(anchor.get('formatted_address'))}</p>"
        f"<p class=\"links\">{_link(anchor.get('google_maps_url'), 'Google Maps')} "
        f"{_link(anchor.get('website'), 'Website')}</p>"
        f"{_photo_meta(anchor)}</section>"
        f"<section><h4>Top competitors</h4>{_top_competitors(profile)}</section>"
        f"<section><h4>Job posting certification demand</h4>"
        f"{_job_demand(profile, scored)}</section>"
        f"<section><h4>Economic profile</h4>{_economic_profile(profile)}</section>"
        f"<section><h4>Accessibility</h4>{_accessibility(profile)}</section>"
        "<section><h4>Rent override</h4>"
        f"<p>rent_score: <strong>{_score(rent.get('rent_score'))}</strong>; "
        f"confidence: {_esc(rent.get('rent_data_confidence'))}; "
        f"source: {_link(rent.get('rent_source'), 'source')}; "
        f"notes: {_esc(rent.get('rent_notes'))}</p></section>"
        "<section><h4>Why this location</h4>"
        f"{_list((scored.get('rationale') or [])[:6], 'no rationale')}</section>"
        "</details>"
    )

    _ANCHOR_BADGE = {AREA_PROXY: "ct-landmark", INVALID_ANCHOR: "ct-invalid"}
    anchor_badge_cls = _ANCHOR_BADGE.get(aa.anchor_status, "ct-verified")
    addr = anchor.get("formatted_address")
    anchor_block = (
        "<div class=\"anchor-block\">"
        f"<div><span>Area</span><strong>{_esc(area_name)}</strong></div>"
        f"<div><span>Anchor</span><strong>{_esc(anchor_name or '—')}</strong>"
        + (f"<br><span class=\"muted\">{_esc(addr)}</span>" if addr else "")
        + "</div>"
        f"<div><span>Anchor status</span><strong>"
        f"<span class=\"badge {anchor_badge_cls}\">{_esc(aa.anchor_status_label)}</span> "
        f"<span class=\"muted\">quality {_esc(aa.anchor_quality_score)}/100</span>"
        "</strong></div>"
        f"<div><span>Lease readiness</span><strong>{_esc(lease_text)}</strong></div>"
        "</div>"
    )
    proxy_checklist = ""
    if aa.anchor_status in (AREA_PROXY, INVALID_ANCHOR):
        items = "".join(f"<li>{_esc(c)}</li>" for c in AREA_PROXY_CHECKLIST)
        proxy_checklist = (
            "<div class=\"proxy-validate\">"
            "<p class=\"muted\">This is an area/landmark proxy, not a confirmed "
            "site. Before treating it as a location:</p>"
            f"<ul class=\"checklist\">{items}</ul></div>"
        )

    card = f"""
    <article class="candidate-card" id="candidate-{_esc(rank)}">
      <header>
        <div class="card-head">
          <p class="eyebrow">Rank #{_esc(rank)}</p>
          <h2>{_esc(area_name)}</h2>
          {anchor_block}
          <p class="badges">
            <span class="badge {_TIER_CLASS.get(tier, 'tier-f')}">Tier {_esc(tier)}</span>
            {_candidate_type_badge(scored)}
            <span class="badge {_READINESS_CLASS.get(readiness, 'rd-weak')}">{_esc(readiness)}</span>
            {_freshness_chip(profile, session_as_of)}
          </p>
          <p class="exec-state">{_esc(scored.get('executive_state') or '')}</p>
          {_validation_chips(scored)}
          {proxy_checklist}
        </div>
        <div class="score-box">
          <strong>{_score(scored.get('area_score'))}</strong>
          <span>area score</span>
          <div class="site-line">Site: {_site_score_display(scored)}</div>
        </div>
      </header>

      <section><h3>Quick read</h3>{_quick_read(interp)}</section>
      <section><h3>Business feasibility</h3>{_feasibility_block(scored)}</section>
      <section><h3>Competition by distance</h3>{_competition_bands(scored)}</section>
      <section><h3>Score meters</h3><div class="meters">{meters}</div></section>
      <section><h3>Historical ALLCPR performance</h3>{_historical_performance_html(profile, scored)}</section>
      <section><h3>ZIP-level course demand</h3>{_zip_demand_html(profile, scored)}</section>
      <section><h3>Expansion readiness</h3>
        <p><span class="badge {_READINESS_CLASS.get(readiness, 'rd-weak')}">{_esc(readiness)}</span></p>
        {readiness_reasons}
      </section>
      <section><h3>Highest-value demand signals</h3>{demand_html}</section>
      <section><h3>Best strategies</h3>{strat_html}</section>
      <section><h3>Competitive interpretation</h3>{comp_html}</section>
      <section><h3>Profitability estimate</h3>{prof_html}</section>
      <section><h3>Warnings</h3>{warnings_html}</section>
      <section><h3>Next-action checklist</h3>{_next_actions_html(scored) or checklist_html}</section>
      {raw_detail}
    </article>
    """
    # Executive view: a field we know nothing about is noise, not honesty —
    # drop unknown/n-a rows entirely (detailed/debug still show every field).
    if report_style == "executive":
        card = _strip_unknown_cells(card)
    return card


# --------------------------------------------------------------------------- #
# Report-level sections
# --------------------------------------------------------------------------- #

def _executive_panel(report_interp: Dict[str, Any]) -> str:
    ev = report_interp.get("executive_verdict")
    if not ev:
        return ("<section class=\"exec-panel\"><h2>Executive verdict</h2>"
                "<p class=\"muted\">No candidates were evaluated.</p></section>")
    rows = [
        (ev.get("best_candidate_label", "Best area-level candidate"), ev["best_candidate"]),
        ("Recommendation", ev.get("executive_state", "")),
        ("Scores", ev.get("score_line", "")),
        ("Verdict", ev["verdict"]),
        ("Expansion readiness", ev["expansion_readiness"]),
        ("Why it matters", ev["why_it_matters"]),
        ("Biggest risk", ev["biggest_risk"]),
        ("Best strategy", ev["best_strategy"]),
        ("Confidence level", ev["confidence"]),
        ("Before leasing", ev["before_leasing"]),
    ]
    body = "".join(
        f"<div class=\"exec-row\"><span>{_esc(k)}</span>"
        f"<strong>{_esc(v)}</strong></div>"
        for k, v in rows
    )
    actions = "".join(
        f"<li>{_esc(a)}</li>" for a in report_interp.get("next_actions") or []
    )
    return (
        "<section class=\"exec-panel\">"
        "<h2>Executive verdict</h2>"
        f"<div class=\"exec-grid\">{body}</div>"
        "<h3>Recommended next 3 actions</h3>"
        f"<ol class=\"next-actions\">{actions}</ol>"
        "</section>"
    )


def _center_recommendation_layer(payload: Dict[str, Any]) -> str:
    co = payload.get("center_opening_recommendations")
    if not co:
        co = build_center_recommendations_from_report(payload, limit=5)
    recs = (co or {}).get("recommendations") or []
    if not recs:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><strong>{_esc(r.get('location_name'))}</strong>"
        f"<br><span class=\"muted\">{_esc(r.get('address'))}</span></td>"
        f"<td>{_esc(r.get('course_type'))}</td>"
        f"<td>{_esc(r.get('area_score'))}</td>"
        f"<td>{_esc(r.get('data_confidence_label'))}<br>"
        f"<span class=\"muted\">Readiness: {_esc(r.get('expansion_readiness'))}</span></td>"
        f"<td><span class=\"badge {_DECISION_CLASS.get(r.get('decision_label'), 'tier-f')}\">"
        f"{_esc(r.get('decision_label'))}</span></td>"
        f"<td>{_esc(r.get('decision_reason'))}<br>"
        f"<span class=\"muted\">{_esc(r.get('suggested_next_action'))}</span></td>"
        "</tr>"
        for r in recs[:5]
    )
    return (
        "<section class=\"summary-section center-rec\">"
        "<h2>Center Opening Recommendation</h2>"
        "<p class=\"muted\">Plain business decision layer. High data confidence "
        "does not mean lease-ready.</p>"
        "<table><thead><tr><th>Location</th><th>Best course</th><th>Area score</th>"
        "<th>Confidence / readiness</th><th>Decision</th><th>Reason / next action</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<p class=\"muted reg-note\">{_esc((co or {}).get('warning_note') or '')}</p>"
        "</section>"
    )


def _toc_nav(context: Dict[str, Any], has_candidates: bool) -> str:
    """Build a sticky table-of-contents that only lists present sections."""
    items: List[tuple] = [("#exec", "Verdict")]
    if context.get("ai_summary"):
        items.append(("#ai", "AI summary"))
    if context.get("city_rankings"):
        items.append(("#areas", "Areas"))
    if context.get("course_performance"):
        items.append(("#course", "Courses"))
    if (context.get("zip_demand_report") or {}).get("rows"):
        items.append(("#zipdemand", "ZIP demand"))
    if has_candidates:
        items.append(("#candidates", "Candidates"))
    items.append(("#sources", "Sources"))
    links = "".join(f'<a href="{href}">{_esc(label)}</a>' for href, label in items)
    return f'<nav class="toc">{links}</nav>'


def _ai_summary_section(context: Dict[str, Any]) -> str:
    """Render the optional AI-generated executive narrative, if present.

    The narrative is produced by an LLM (OpenAI/Groq) from the deterministic
    report data only; it is clearly labeled so it is never mistaken for a new
    source of figures.
    """
    ai = context.get("ai_summary")
    if not ai or not ai.get("text"):
        return ""
    provider = ai.get("provider") or "llm"
    model = ai.get("model") or ""
    paragraphs = "".join(
        f"<p>{_esc(p.strip())}</p>"
        for p in str(ai["text"]).split("\n") if p.strip()
    )
    return (
        "<section class=\"summary-section\">"
        "<h2>AI executive summary</h2>"
        "<div class=\"accuracy\">AI-generated narrative — rephrases the "
        "deterministic analysis below; introduces no new figures. "
        f"Generated by {_esc(provider)}/{_esc(model)}.</div>"
        f"{paragraphs}</section>"
    )


def _city_rankings(context: Dict[str, Any]) -> str:
    rankings = context.get("city_rankings") or []
    if not rankings:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_esc(row.get('city_rank'))}</td>"
        f"<td>{_esc(row.get('area'))}</td>"
        f"<td>{_esc(row.get('best_candidate'))}</td>"
        f"<td>{_score(row.get('best_site_score'))}</td>"
        f"<td>{_score(row.get('avg_top_site_score'))}</td>"
        f"<td>{_esc(row.get('candidate_count'))}</td>"
        "</tr>"
        for row in rankings
    )
    return (
        "<section class=\"summary-section\"><h2>City / area ranking</h2>"
        "<table><thead><tr><th>Rank</th><th>Area</th><th>Best candidate</th>"
        "<th>Best score</th><th>Avg top score</th><th>Candidates</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></section>"
    )


_BAND_CLASS = {"Strong": "rd-strong", "Average": "rd-moderate",
               "Weak": "rd-weak", "Unknown": "tier-f"}


def _pct(value: Any) -> str:
    return f"{value:.0f}%" if isinstance(value, (int, float)) else "unknown"


_TREND_DIR_CLASS = {
    "improving": "rd-strong",
    "declining": "rd-weak",
    "flat": "tier-c",
    "insufficient data": "tier-f",
}
_TREND_DIR_COLOR = {
    "improving": "var(--good)",
    "declining": "var(--low)",
    "flat": "var(--mid)",
    "insufficient data": "var(--muted)",
}


def _period_abs_index(period: str) -> float:
    """Absolute time index for spacing dots: month-number for YYYY-MM,
    ordinal day for YYYY-MM-DD. Only used for relative positioning."""
    parts = period.split("-")
    if len(parts) >= 3:
        from datetime import date
        return float(date(int(parts[0]), int(parts[1]), int(parts[2])).toordinal())
    return float(int(parts[0]) * 12 + int(parts[1]))


def _course_trend_card_svg(trend: Dict[str, Any]) -> str:
    """Compact per-course regression card: monthly-average dots + least-squares
    trend line. Enrollware history only; no JS, pure SVG so it survives offline."""
    points = [p for p in (trend.get("points") or [])
              if isinstance(p.get("average_enrollment"), (int, float))]
    direction = trend.get("trend_direction") or "insufficient data"
    color = _TREND_DIR_COLOR.get(direction, "var(--muted)")
    if not points:
        return "<p class=\"muted\">No dated Enrollware history for this course.</p>"

    left, right, top, bottom = 36.0, 348.0, 14.0, 120.0
    plot_w, plot_h = right - left, bottom - top
    idx = [_period_abs_index(p["period"]) for p in points]
    ys = [float(p["average_enrollment"]) for p in points]
    tmin, tmax = min(idx), max(idx)
    if tmax - tmin < 1e-9:
        tmin, tmax = tmin - 1.0, tmax + 1.0
    ymax = max(ys) * 1.15 if max(ys) > 0 else 1.0

    def px(t: float) -> float:
        return left + (t - tmin) / (tmax - tmin) * plot_w

    def py(y: float) -> float:
        return bottom - (y / ymax) * plot_h

    parts = [
        f"<rect x='{left:.0f}' y='{top:.0f}' width='{plot_w:.0f}' "
        f"height='{plot_h:.0f}' fill='#fff' stroke='var(--line)' />"
    ]
    for i in (1, 2, 3):
        gy = top + plot_h * i / 4.0
        val = ymax - ymax * i / 4.0
        parts.append(
            f"<line x1='{left:.0f}' y1='{gy:.1f}' x2='{right:.0f}' y2='{gy:.1f}' "
            f"stroke='var(--line)' stroke-dasharray='3 3' />"
            f"<text x='{left - 4:.0f}' y='{gy + 3:.1f}' text-anchor='end' "
            f"font-size='8' fill='var(--muted)'>{val:.1f}</text>"
        )
    parts.append(
        f"<text x='{left - 4:.0f}' y='{top + 4:.0f}' text-anchor='end' "
        f"font-size='8' fill='var(--muted)'>{ymax:.1f}</text>"
        f"<text x='{left - 4:.0f}' y='{bottom + 3:.0f}' text-anchor='end' "
        f"font-size='8' fill='var(--muted)'>0</text>"
    )

    # Regression line through the centroid (basis-independent): the least-squares
    # line always passes through (x̄, ȳ) with the fitted slope.
    slope = trend.get("slope")
    if isinstance(slope, (int, float)) and len(points) >= 3:
        xbar = sum(idx) / len(idx)
        ybar = sum(ys) / len(ys)
        y1 = ybar + float(slope) * (tmin - xbar)
        y2 = ybar + float(slope) * (tmax - xbar)
        y1c, y2c = max(0.0, min(ymax, y1)), max(0.0, min(ymax, y2))
        parts.append(
            f"<line x1='{px(tmin):.1f}' y1='{py(y1c):.1f}' x2='{px(tmax):.1f}' "
            f"y2='{py(y2c):.1f}' stroke='{color}' stroke-width='2.5' />"
        )

    # x end labels (first / last period).
    parts.append(
        f"<text x='{left:.0f}' y='{bottom + 12:.0f}' text-anchor='start' "
        f"font-size='8' fill='var(--muted)'>{_esc(points[0]['period'])}</text>"
        f"<text x='{right:.0f}' y='{bottom + 12:.0f}' text-anchor='end' "
        f"font-size='8' fill='var(--muted)'>{_esc(points[-1]['period'])}</text>"
    )

    # Dots (last so they sit on the line) with hover tooltips.
    for p, t, y in zip(points, idx, ys):
        cx, cy = px(t), py(y)
        title = (
            f"{trend.get('course_type')} | {p['period']} | "
            f"Avg {y:.2f}/class | Classes {p.get('class_count') or 0} | "
            f"Trend {direction}"
        )
        parts.append(
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='3.2' fill='{color}' "
            f"fill-opacity='0.85' stroke='#fff' stroke-width='1'>"
            f"<title>{_esc(title)}</title></circle>"
        )

    return (
        "<svg class=\"trend-svg\" viewBox=\"0 0 360 136\" "
        "xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        f"aria-label=\"{_esc(str(trend.get('course_type')))} monthly enrollment "
        "trend\">" + "".join(parts) + "</svg>"
    )


def _course_trend_card_html(trend: Dict[str, Any]) -> str:
    direction = trend.get("trend_direction") or "insufficient data"
    badge_cls = _TREND_DIR_CLASS.get(direction, "tier-f")

    def _num(v: Any, fmt: str = "{:.3f}") -> str:
        return fmt.format(float(v)) if isinstance(v, (int, float)) else "—"

    stats = (
        f"<div><span>Direction</span><strong>"
        f"<span class=\"badge {badge_cls}\">{_esc(direction)}</span></strong></div>"
        f"<div><span>Slope / month</span><strong>{_num(trend.get('slope'), '{:+.3f}')}"
        f"</strong></div>"
        f"<div><span>R²</span><strong>{_num(trend.get('r_squared'))}</strong></div>"
        f"<div><span>Pearson</span><strong>{_num(trend.get('pearson'))}</strong></div>"
        f"<div><span>Avg students/class</span><strong>"
        f"{_num(trend.get('average_students_per_class'), '{:.2f}')}</strong></div>"
        f"<div><span>Classes</span><strong>{_esc(trend.get('total_classes') or 0)}"
        f"</strong></div>"
    )
    return (
        "<div class=\"trend-card\">"
        f"<h4>{_esc(trend.get('course_type'))} "
        f"<span class=\"muted\">· {_esc(trend.get('n') or 0)} pts · "
        f"{_esc(trend.get('confidence_label'))}</span></h4>"
        + _course_trend_card_svg(trend)
        + f"<div class=\"trend-stats\">{stats}</div>"
        + f"<p class=\"muted trend-note\">{_esc(trend.get('business_note'))}</p>"
        "</div>"
    )


def _course_trends_html(trends_payload: Optional[Dict[str, Any]]) -> str:
    """Render the 'Historical Enrollment Trend by Course Type' section: one
    compact regression card per course type (ARC CPR, ARC BLS, AHA BLS)."""
    if not trends_payload:
        return ""
    trends = trends_payload.get("trends") or []
    if not trends:
        return ""
    cards = "".join(_course_trend_card_html(t) for t in trends)
    cutoff_note = ""
    if trends_payload.get("note"):
        cutoff_note = f" {_esc(trends_payload['note'])}"
    return (
        "<section class=\"summary-section\">"
        "<h2>Historical Enrollment Trend by Course Type</h2>"
        "<p class=\"muted\">Least-squares regression of monthly average "
        "enrollment over time, per course type. Enrollware history only — no "
        "Google, Census, Yelp, Foursquare, or Adzuna signals. A historical "
        "direction signal, not a guaranteed forecast." + cutoff_note + "</p>"
        f"<div class=\"trend-grid\">{cards}</div>"
        "</section>"
    )


def _course_benchmark_html(bench: Optional[Dict[str, Any]]) -> str:
    if not bench:
        return ""
    overall = bench.get("allcpr_overall_average")

    def _fill(v: Any) -> str:
        return f"{float(v):.0f}%" if isinstance(v, (int, float)) else "—"

    rows = [
        "<tr class=\"benchmark-row\"><td><strong>ALLCPR overall average</strong></td>"
        f"<td>{_score(overall)}</td>"
        f"<td>{_fill(bench.get('allcpr_overall_fill_rate_pct'))}</td>"
        f"<td>{_esc(bench.get('allcpr_total_students') or '')}</td>"
        f"<td>{_esc(bench.get('allcpr_class_count') or '')}</td>"
        "<td>—</td><td>—</td><td>Company-wide Enrollware benchmark.</td></tr>"
    ]
    for row in bench.get("course_benchmarks") or []:
        diff = row.get("difference_vs_allcpr_average")
        pct = row.get("percent_vs_allcpr_average")
        diff_text = "—" if diff is None else f"{float(diff):+.2f}"
        pct_text = "—" if pct is None else f"{float(pct):+.1f}%"
        conf_note = "Low sample" if row.get("data_confidence") == "low" else ""
        conf_html = (
            f"<br><span class=\"muted\">{_esc(conf_note)}</span>"
            if conf_note else ""
        )
        rows.append(
            "<tr>"
            f"<td><strong>{_esc(row.get('course_type'))}</strong>"
            f"{conf_html}</td>"
            f"<td>{_score(row.get('average_students_per_class'))}</td>"
            f"<td>{_fill(row.get('average_fill_rate_pct'))}</td>"
            f"<td>{_esc(row.get('total_students') or '')}</td>"
            f"<td>{_esc(row.get('class_count') or 0)}</td>"
            f"<td>{_esc(diff_text)}</td>"
            f"<td>{_esc(pct_text)}</td>"
            f"<td>{_esc(row.get('recommendation_note'))}</td>"
            "</tr>"
        )
    return (
        "<section class=\"summary-section\">"
        "<h2>Historical Enrollment by Course Type</h2>"
        "<p class=\"muted\">Enrollware-only benchmark. No Google, Census, Yelp, "
        "Foursquare, or Adzuna signals are used here. Counts <em>held</em> "
        "classes only — classes that ran with real attendance in a completed "
        "month; cancelled/zero-enrollment placeholders and future/partial "
        "months are excluded so the averages are not understated.</p>"
        + "<table><thead><tr><th>Course type</th><th>Avg students/class</th>"
        "<th>Fill rate</th><th>Total students</th><th>Class count</th>"
        "<th>Diff vs ALLCPR avg</th><th>% vs ALLCPR avg</th>"
        "<th>Recommendation note</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</section>"
    )


def _data_quality_html(dq: Optional[Dict[str, Any]]) -> str:
    """Render the Enrollware data-quality summary (cleaning report).

    Surfaces what the loader cleaned/flagged so the files are transparently
    handled, never silently rejected. Hidden when no Enrollware load happened.
    """
    if not dq or not dq.get("classes_loaded"):
        return ""

    def cell(label: str, value: Any, warn: bool = False) -> str:
        cls = " dq-warn" if warn and value else ""
        return (f"<div class=\"dq-item{cls}\"><span>{_esc(label)}</span>"
                f"<strong>{_esc(value)}</strong></div>")

    dups = dq.get("duplicate_abbreviations") or {}
    amb = dq.get("ambiguous_abbreviations") or []
    dup_txt = ", ".join(f"{k} ×{v}" for k, v in list(dups.items())[:8]) or "none"
    amb_txt = ", ".join(amb[:8]) or "none"

    classes = "".join([
        cell("Class rows loaded", dq.get("classes_loaded")),
        cell("Blank rows ignored", dq.get("classes_blank_ignored")),
        cell("Held classes (avg basis)", dq.get("held_classes")),
        cell("Zero-student rows", dq.get("zero_student_rows")),
        cell("Missing location", dq.get("missing_location"), warn=True),
        cell("Missing start date", dq.get("missing_start_date"), warn=True),
        cell("Missing end date", dq.get("missing_end_date"), warn=True),
        cell("Missing hours", dq.get("missing_hours"), warn=True),
        cell("Unmatched locations", dq.get("unmatched_locations"), warn=True),
        cell("Ambiguous-location rows", dq.get("ambiguous_location_rows"), warn=True),
        cell("Capacity overfilled (students > seats)",
             dq.get("capacity_overfilled"), warn=True),
        cell("Seats = 0 but students > 0",
             dq.get("zero_seats_with_students"), warn=True),
    ])
    locations = "".join([
        cell("Location rows loaded", dq.get("locations_loaded")),
        cell("Blank rows ignored", dq.get("locations_blank_ignored")),
        cell("Missing abbreviation", dq.get("locations_missing_abbreviation"),
             warn=True),
        cell("Duplicate abbreviations", len(dups), warn=True),
        cell("Ambiguous abbreviations", len(amb), warn=True),
    ])
    return (
        "<section class=\"summary-section\">"
        "<h2>Enrollware Data Quality</h2>"
        "<p class=\"muted\">The Classes and Locations files are cleaned, not "
        "rejected. Blank formatted rows are ignored, location names are "
        "normalized before joining (e.g. “San Jose (t)” → “San Jose”), "
        "duplicate/ambiguous abbreviations are not force-resolved to a guessed "
        "city, and zero-student / capacity anomalies are flagged below.</p>"
        "<h3>Classes</h3>"
        f"<div class=\"dq-grid\">{classes}</div>"
        "<h3>Locations</h3>"
        f"<div class=\"dq-grid\">{locations}</div>"
        f"<p class=\"muted\">Duplicate abbreviations: {_esc(dup_txt)}. "
        f"Ambiguous (multiple cities, city left blank): {_esc(amb_txt)}. "
        "Zero-student rows are kept for cancellation / low-enrollment risk but "
        "excluded from filled-class averages.</p>"
        "</section>"
    )


def _course_performance_section(context: Dict[str, Any]) -> str:
    """Render the Phase 4B course-type evaluation sections.

    Renders a placeholder when no Enrollware data is loaded so the report
    always documents the capability; renders the four real sections
    (historical performance, best course strategy, public demand vs actual,
    scheduling) when ``context['course_performance']`` is populated.
    """
    perf = context.get("course_performance")
    if not perf:
        return (
            "<section class=\"summary-section\">"
            "<h2>Course Opportunity Graph</h2>"
            "<p class=\"muted\">No Enrollware class history loaded. Drop an export "
            "at <code>data/raw/enrollware_classes.xlsx</code> and re-run to rank "
            "courses to push, keep, or only test.</p>"
            "</section>"
        )

    course_types = perf.get("course_types") or []
    overall = perf.get("overall") or {}
    area = perf.get("area_label") or "ALLCPR-wide"
    scope = "" if perf.get("area_is_filtered") else (
        " <span class=\"muted\" title=\"Export had no matching city/location "
        "column for this area\">(ALLCPR-wide)</span>"
    )

    # 1. Historical benchmark table (ALLCPR-wide) + per-course trend cards.
    benchmark = _course_benchmark_html(perf.get("course_enrollment_benchmarks"))
    trends = _course_trends_html(perf.get("course_enrollment_trends"))

    # 2. Historical performance table.
    rows = "".join(
        "<tr>"
        f"<td>{_esc(ct.get('label'))} "
        f"<span class=\"badge {_BAND_CLASS.get(ct.get('performance_band'), 'tier-f')}\">"
        f"{_esc(ct.get('performance_band'))}</span></td>"
        f"<td>{_esc(ct.get('total_classes'))}</td>"
        f"<td>{_esc(ct.get('classes_held'))}</td>"
        f"<td>{_esc(ct.get('total_students'))}</td>"
        f"<td>{_esc(ct.get('average_students_per_class'))}</td>"
        f"<td>{_esc(ct.get('average_students_per_held_class'))}</td>"
        f"<td>{_esc(ct.get('median_students_per_class'))}</td>"
        f"<td>{_pct(ct.get('fill_rate_percent'))}</td>"
        f"<td>{_score(ct.get('course_performance_score'))}</td>"
        f"<td>{_money(ct.get('revenue_estimate'))}</td>"
        "</tr>"
        for ct in course_types
    )
    modeled = perf.get("modeled_price")
    rev_note = (
        f"Revenue modeled at ALLCPR median ${modeled:,.0f}/student (no price "
        "in export)."
        if modeled and not (perf.get("data_coverage") or {}).get("price")
        else "Revenue = observed price × enrollment."
    )
    has_hybrid = any(
        ct.get("course_type") == HYBRID_COURSE_KEY for ct in course_types
    )
    hybrid_note = (
        f"<p class=\"muted\">{_esc(HYBRID_COURSE_NOTE)}</p>" if has_hybrid else ""
    )
    hist = (
        f"<section class=\"summary-section\"><h2>Historical ALLCPR Course "
        f"Performance</h2><p class=\"muted\">{_esc(area)}{scope} · "
        f"{_esc(overall.get('total_classes'))} classes · avg "
        f"{_esc(overall.get('average_students_per_class'))} students/class. "
        "Blank = unknown, never estimated.</p>"
        "<table><thead><tr><th>Course type</th><th>Classes</th><th>Held</th>"
        "<th>Students</th><th>Avg/class</th><th>Avg (held)</th><th>Median</th>"
        "<th>Fill rate</th><th>Perf. score</th><th>Revenue est.</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class=\"muted\">{_esc(rev_note)}</p>{hybrid_note}</section>"
    )

    # 2. Best course strategy. Powered by the Phase 5 evaluation graph when
    # available; otherwise falls back to the existing deterministic strategy.
    eval_graph = perf.get("evaluation_graph")
    if eval_graph:
        strat = {
            "primary": eval_graph.get("primary") or [],
            "secondary": eval_graph.get("secondary") or [],
            "avoid_or_test": eval_graph.get("avoid_or_test") or [],
            "verdicts": eval_graph.get("explanations")
            or (perf.get("strategy") or {}).get("verdicts") or [],
        }
    else:
        strat = perf.get("strategy") or {}

    def _chips(labels: List[str], cls: str) -> str:
        if not labels:
            return "<span class=\"muted\">none</span>"
        return "".join(
            f"<span class=\"badge {cls}\">{_esc(l)}</span> " for l in labels
        )

    verdicts = "".join(
        f"<li>{_esc(v)}</li>" for v in (strat.get("verdicts") or [])
    )
    strategy = (
        "<section class=\"summary-section\"><h2>Best Course Strategy for This "
        "Area</h2><div class=\"interp-grid\">"
        f"<div><span>Primary</span><strong>{_chips(strat.get('primary') or [], 'rd-strong')}</strong></div>"
        f"<div><span>Secondary</span><strong>{_chips(strat.get('secondary') or [], 'rd-moderate')}</strong></div>"
        f"<div><span>Avoid / test only</span><strong>{_chips(strat.get('avoid_or_test') or [], 'rd-weak')}</strong></div>"
        "</div>"
        + (f"<ul>{verdicts}</ul>" if verdicts else
           "<p class=\"muted\">Not enough scored course types to rank yet.</p>")
        + "</section>"
    )

    # 3. Public demand vs actual enrollment.
    pdva = perf.get("public_demand_vs_actual") or {}
    pdva_notes = "".join(f"<li>{_esc(n)}</li>" for n in (pdva.get("notes") or []))
    public = (
        "<section class=\"summary-section\"><h2>Public Demand vs Actual "
        "Enrollment</h2>"
        "<div class=\"interp-grid\">"
        f"<div><span>BLS-driving demand sites</span><strong>{_esc(pdva.get('bls_demand_sites'))}</strong></div>"
        f"<div><span>CPR-driving demand sites</span><strong>{_esc(pdva.get('cpr_demand_sites'))}</strong></div>"
        f"<div><span>BLS actual avg students</span><strong>{_esc(pdva.get('bls_actual_avg_students'))}</strong></div>"
        f"<div><span>CPR actual avg students</span><strong>{_esc(pdva.get('cpr_actual_avg_students'))}</strong></div>"
        "</div>"
        + (f"<ul>{pdva_notes}</ul>" if pdva_notes else
           "<p class=\"muted\">No external demand signals supplied to compare "
           "against enrollment.</p>")
        + "</section>"
    ) if pdva else ""

    # 4. Scheduling recommendation.
    sched_items = "".join(
        f"<li>{_esc(s)}</li>" for s in (perf.get("scheduling_recommendations") or [])
    )
    scheduling = (
        "<section class=\"summary-section\"><h2>Scheduling Recommendation</h2>"
        f"<ul class=\"checklist\">{sched_items}</ul></section>"
        if sched_items else ""
    )

    # 5. Schedule intelligence — best day / month / day-part learned from history.
    schedule_intel = _schedule_intelligence_html(perf.get("schedule_intelligence"))

    # 6. Forecast — recency-weighted expected students / fill / revenue.
    forecast = _forecast_html(perf.get("forecast"))

    # 7. Phase 5 — Course Opportunity Graph (honest weighted evidence).
    evaluation = _evaluation_graph_html(perf.get("evaluation_graph"))

    # 8. Score vs Actual Enrollment Validation — sits directly under the
    # Course Opportunity Graph. Honest sanity-check, never a forecast.
    regression = _regression_validation_html(perf.get("regression_validation"))

    # 9. Center-opening decision table — one row per course, no new scoring.
    center = _center_opening_html(perf.get("center_opening"))

    if eval_graph:
        # The graph's recommendation strip replaces the standalone strategy
        # section; every detailed table folds away so the default view stays
        # the visual graph + the headline answer.
        details = hist + public + scheduling + schedule_intel + forecast
        return (
            evaluation
            + center
            + benchmark
            + trends
            + regression
            + "<details class=\"summary-section course-details\">"
            "<summary>Course performance details — history, demand, "
            "scheduling &amp; forecast</summary>"
            f"<div class=\"course-details-body\">{details}</div></details>"
        )

    # No graph: keep the original flat layout (with the strategy fallback). The
    # validation sits right after the strategy block (its opportunity-score
    # surrogate) so it stays the layer under the course-opportunity content.
    return (
        hist + strategy + center + benchmark + trends + regression + public
        + scheduling + schedule_intel + forecast
    )


def _schedule_intelligence_html(si: Optional[Dict[str, Any]]) -> str:
    """Render best day / month / weekend-vs-weekday from class history."""
    if not si:
        return ""

    def _slot(label: str, bucket: Optional[Dict[str, Any]]) -> str:
        if not bucket:
            return f"<div><span>{label}</span><strong>unknown</strong></div>"
        avg = bucket.get("average_students_per_class")
        detail = (
            f"{_esc(bucket.get('average_students_per_class'))} avg"
            if bucket.get("basis") == "enrollment" and avg is not None
            else f"{_esc(bucket.get('classes'))} classes (by volume)"
        )
        return (
            f"<div><span>{label}</span>"
            f"<strong>{_esc(bucket.get('label'))}</strong>"
            f"<span class=\"muted\"> — {detail}</span></div>"
        )

    ww = si.get("weekend_vs_weekday") or {}
    wd = (ww.get("weekday") or {}).get("average_students_per_class")
    we = (ww.get("weekend") or {}).get("average_students_per_class")
    recs = "".join(f"<li>{_esc(r)}</li>" for r in (si.get("recommendations") or []))
    return (
        "<section class=\"summary-section\"><h2>Schedule Intelligence</h2>"
        "<div class=\"interp-grid\">"
        + _slot("Best day", si.get("best_day"))
        + _slot("Best month", si.get("best_month"))
        + f"<div><span>Weekday avg</span><strong>{_esc(wd)}</strong></div>"
        + f"<div><span>Weekend avg</span><strong>{_esc(we)}</strong></div>"
        + "</div>"
        + (f"<ul class=\"checklist\">{recs}</ul>" if recs else "")
        + "<p class=\"muted\">Class start time is not in the Enrollware export, "
        "so time-of-day is left unknown.</p></section>"
    )


def _forecast_html(fc: Optional[Dict[str, Any]]) -> str:
    """Render the recency-weighted per-course-type forecast."""
    if not fc:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_esc(c.get('label'))}</td>"
        f"<td>{_esc(c.get('sample_size'))}</td>"
        f"<td>{_esc(c.get('expected_students'))}</td>"
        f"<td>{_pct(c.get('expected_fill_rate_percent'))}</td>"
        f"<td>{_money(c.get('expected_revenue'))}</td>"
        f"<td>{_esc(c.get('confidence'))}</td>"
        "</tr>"
        for c in (fc.get("course_types") or [])
    )
    overall = fc.get("overall") or {}
    return (
        "<section class=\"summary-section\"><h2>Forecast — Next-Class Expectation</h2>"
        f"<p class=\"muted\">Recency-weighted from ALLCPR's own history "
        f"({_esc(fc.get('half_life_months'))}-mo half-life) — not ML. "
        "Blank = unknown.</p>"
        "<table><thead><tr><th>Course type</th><th>Sample</th>"
        "<th>Expected students</th><th>Expected fill</th>"
        "<th>Expected revenue</th><th>Confidence</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class=\"muted\">Overall expected students/class: "
        f"{_esc(overall.get('expected_students'))}.</p></section>"
    )


_EVAL_NODE_COLUMNS = [
    ("historical_performance", "Historical"),
    ("public_demand", "Public demand"),
    ("course_relative_performance", "Course vs avg"),
    ("competition_gap", "Competition"),
    ("schedule_strength", "Schedule"),
    ("forecast_expected_students", "Forecast"),
]

_EVAL_GROUP_CLASS = {
    "Primary": "rd-strong",
    "Secondary": "rd-moderate",
    "Avoid / test only": "rd-weak",
}

# Distinct colour per evidence component for the stacked contribution chart.
_EVAL_NODE_COLORS = {
    "historical_performance": "#2a8f5f",
    "public_demand": "#3b7dd8",
    "course_relative_performance": "#8e5bd0",
    "competition_gap": "#d98a1f",
    "schedule_strength": "#1fa8a8",
    "forecast_expected_students": "#c2557a",
}
_EVAL_PENALTY_COLOR = "#b4453a"


def _eval_legend() -> str:
    """Colour key for the stacked contribution chart."""
    items = "".join(
        f"<span class=\"eval-legend-item\"><span class=\"eval-swatch\" "
        f"style=\"background:{_EVAL_NODE_COLORS[key]}\"></span>{_esc(lbl)}</span>"
        for key, lbl in _EVAL_NODE_COLUMNS
    )
    items += (
        "<span class=\"eval-legend-item\"><span class=\"eval-swatch eval-penalty-"
        f"swatch\" style=\"background:{_EVAL_PENALTY_COLOR}\"></span>Penalty</span>"
    )
    return f"<div class=\"eval-legend\">{items}</div>"


def _eval_contribution_chart(courses: List[Dict[str, Any]]) -> str:
    """Stacked horizontal bars: each course's score broken into its drivers.

    The coloured segments are the positive contributions (summing to the
    pre-penalty total); a hatched red segment shows the confidence penalty
    eaten off the end. Track width is 0..100 points. No JS, no libraries.
    """
    rows = ""
    for c in courses:
        segments = ""
        for key, _ in _EVAL_NODE_COLUMNS:
            node = next((n for n in (c.get("nodes") or []) if n["key"] == key), None)
            if not node or node.get("missing"):
                continue
            contrib = float(node.get("contribution") or 0)
            if contrib <= 0:
                continue
            color = _EVAL_NODE_COLORS.get(key, "#888")
            title = f"{node.get('label')}: +{contrib:.0f}"
            segments += (
                f"<div class=\"eval-seg\" style=\"width:{contrib:.1f}%;"
                f"background:{color}\" title=\"{_esc(title)}\"></div>"
            )
        penalty = float((c.get("penalty") or {}).get("penalty_points") or 0)
        if penalty > 0:
            segments += (
                f"<div class=\"eval-seg eval-seg-penalty\" "
                f"style=\"width:{penalty:.1f}%\" "
                f"title=\"Confidence penalty: -{penalty:.0f}\"></div>"
            )
        group = c.get("display_group", "")
        badge_cls = _EVAL_GROUP_CLASS.get(group, "tier-f")
        rows += (
            "<div class=\"eval-row\">"
            f"<div class=\"eval-row-label\"><strong>{_esc(c.get('label'))}</strong>"
            f"<span class=\"badge {badge_cls}\">{_esc(group)}</span></div>"
            f"<div class=\"eval-stack\">{segments}</div>"
            f"<div class=\"eval-row-score\">{_esc(c.get('final_score'))}</div>"
            "</div>"
        )
    return (
        "<div class=\"eval-chart\">" + rows + "</div>" + _eval_legend()
    )


def _eval_score_bar(score: float) -> str:
    """A dependency-free horizontal bar (inline CSS width %, no JS)."""
    pct = max(0.0, min(100.0, float(score or 0)))
    return (
        "<div style=\"background:#e9edf2;border-radius:5px;height:11px;"
        "width:120px;display:inline-block;vertical-align:middle\">"
        f"<div style=\"width:{pct:.0f}%;height:11px;border-radius:5px;"
        "background:linear-gradient(90deg,#2a8f5f,#4caf7d)\"></div></div>"
    )


def _evaluation_graph_html(graph: Optional[Dict[str, Any]]) -> str:
    """Render the Phase 5 Course Opportunity Graph (deterministic, explainable).

    One row per course type: final score (with a horizontal bar), recommendation
    group, and the per-component contributions; below the table, the plain-English
    "Why this recommendation?" reasons. Missing components render as ``—`` — never
    a fabricated number.
    """
    if not graph:
        return ""
    courses = graph.get("course_opportunity_graph") or []
    if not courses:
        return ""

    def _chips(labels: Optional[List[str]], cls: str) -> str:
        labels = labels or []
        if not labels:
            return "<span class=\"muted\">—</span>"
        return "".join(
            f"<span class=\"badge {cls}\">{_esc(l)}</span> " for l in labels)

    # Headline answer: which courses to push / keep / only test.
    strip = (
        "<div class=\"eval-rec\">"
        f"<div class=\"eval-rec-cell rd-tint-strong\"><span>Push</span>"
        f"<div>{_chips(graph.get('primary'), 'rd-strong')}</div></div>"
        f"<div class=\"eval-rec-cell rd-tint-mod\"><span>Keep</span>"
        f"<div>{_chips(graph.get('secondary'), 'rd-moderate')}</div></div>"
        f"<div class=\"eval-rec-cell rd-tint-weak\"><span>Avoid / test</span>"
        f"<div>{_chips(graph.get('avoid_or_test'), 'rd-weak')}</div></div>"
        "</div>"
    )

    # One concise line per course (top driver), plus a foldaway number table.
    head_cells = "".join(f"<th>{_esc(lbl)}</th>" for _, lbl in _EVAL_NODE_COLUMNS)
    why_rows = ""
    table_rows = ""
    for c in courses:
        nodes = {n["key"]: n for n in c.get("nodes") or []}
        present = [n for n in (c.get("nodes") or []) if not n.get("missing")]
        top = max(present, key=lambda n: n.get("contribution", 0), default=None)
        driver = (f"led by {top['label'].lower()}" if top else "limited evidence")
        pen = (c.get("penalty") or {}).get("penalty_points", 0) or 0
        pen_txt = f" · penalty −{pen:.0f}" if pen else ""
        group = c.get("display_group", "")
        badge_cls = _EVAL_GROUP_CLASS.get(group, "tier-f")
        why_rows += (
            "<li>"
            f"<span class=\"badge {badge_cls}\">{_esc(group)}</span> "
            f"<strong>{_esc(c.get('label'))}</strong> "
            f"<span class=\"eval-score-pill\">{_esc(c.get('final_score'))}</span>"
            f"<span class=\"muted\"> — {_esc(driver)}{_esc(pen_txt)}</span></li>"
        )
        contrib_cells = ""
        for key, _ in _EVAL_NODE_COLUMNS:
            node = nodes.get(key)
            contrib_cells += (
                "<td class=\"muted\">—</td>" if (not node or node.get("missing"))
                else f"<td>+{node.get('contribution', 0):.0f}</td>")
        table_rows += (
            "<tr>"
            f"<td><strong>{_esc(c.get('label'))}</strong></td>"
            f"<td><strong>{_esc(c.get('final_score'))}</strong></td>"
            f"<td><span class=\"badge {badge_cls}\">{_esc(group)}</span></td>"
            f"{contrib_cells}<td>−{pen:.0f}</td></tr>"
        )

    notes = graph.get("confidence_notes") or []
    notes_html = (
        f"<p class=\"muted eval-note\">⚠ {_esc(notes[0])}</p>" if notes else ""
    )
    table = (
        "<details class=\"eval-details\"><summary>Score breakdown (numbers)</summary>"
        "<table><thead><tr><th>Course</th><th>Score</th><th>Rec.</th>"
        f"{head_cells}<th>Pen.</th></tr></thead>"
        f"<tbody>{table_rows}</tbody></table></details>"
    )
    return (
        "<section class=\"summary-section\"><h2>Course Opportunity Graph</h2>"
        "<p class=\"muted eval-cap\">What drives each course's score — missing "
        "signals are left out, not assumed.</p>"
        + strip
        + _eval_contribution_chart(courses)
        + f"<ul class=\"eval-why-list\">{why_rows}</ul>"
        + notes_html
        + table
        + "</section>"
    )


def _reg_stat(label: str, value: Any, *, kind: str = "num") -> str:
    """One labelled stat pill for the regression read-out (R², slope, …)."""
    if value is None:
        shown = "—"
    elif kind == "int":
        shown = f"{int(value)}"
    else:
        shown = f"{float(value):+.3f}" if kind == "signed" else f"{float(value):.3f}"
    return (
        "<div class=\"reg-stat\"><span class=\"reg-stat-label\">"
        f"{_esc(label)}</span><strong>{_esc(shown)}</strong></div>"
    )


def _regression_scatter_svg(rv: Dict[str, Any]) -> str:
    """Dependency-free SVG scatter + (when n>=3) least-squares regression line.

    x = opportunity score, y = actual historical enrollment. One dot per usable
    course point. No JS, no chart libraries — pure SVG so it survives offline
    HTML and the existing no-dependency report style.
    """
    points = [p for p in (rv.get("points") or [])
              if isinstance(p.get("score"), (int, float))
              and isinstance(p.get("actual_enrollment"), (int, float))]
    if not points:
        return ""

    left, right, top, bottom = 58.0, 540.0, 18.0, 268.0
    plot_w, plot_h = right - left, bottom - top

    xs = [float(p["score"]) for p in points]
    ys = [float(p["actual_enrollment"]) for p in points]
    xmin, xmax = min(xs), max(xs)
    if xmax - xmin < 1e-9:
        xmin, xmax = xmin - 1.0, xmax + 1.0
    ymin = 0.0
    ymax = max(ys) * 1.15 if max(ys) > 0 else 1.0

    def px(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * plot_w

    def py(y: float) -> float:
        return bottom - (y - ymin) / (ymax - ymin) * plot_h

    # Frame + light gridlines (4 horizontal bands).
    parts = [
        f"<rect x='{left:.0f}' y='{top:.0f}' width='{plot_w:.0f}' "
        f"height='{plot_h:.0f}' fill='#fff' stroke='var(--line)' />"
    ]
    for i in range(1, 4):
        gy = top + plot_h * i / 4.0
        val = ymax - (ymax - ymin) * i / 4.0
        parts.append(
            f"<line x1='{left:.0f}' y1='{gy:.1f}' x2='{right:.0f}' y2='{gy:.1f}' "
            f"stroke='var(--line)' stroke-dasharray='3 3' />"
        )
        parts.append(
            f"<text x='{left - 6:.0f}' y='{gy + 3:.1f}' text-anchor='end' "
            f"font-size='10' fill='var(--muted)'>{val:.1f}</text>"
        )
    # y top/bottom labels.
    parts.append(
        f"<text x='{left - 6:.0f}' y='{top + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>{ymax:.1f}</text>"
    )
    parts.append(
        f"<text x='{left - 6:.0f}' y='{bottom + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>0</text>"
    )
    # x min/max labels.
    parts.append(
        f"<text x='{left:.0f}' y='{bottom + 16:.0f}' text-anchor='middle' "
        f"font-size='10' fill='var(--muted)'>{xmin:.0f}</text>"
    )
    parts.append(
        f"<text x='{right:.0f}' y='{bottom + 16:.0f}' text-anchor='middle' "
        f"font-size='10' fill='var(--muted)'>{xmax:.0f}</text>"
    )

    # Regression line — only when we genuinely fit one (n>=3 + score spread).
    if rv.get("enough_data") and rv.get("slope") is not None:
        slope = float(rv["slope"])
        intercept = float(rv["intercept"])

        def clamp_y(y: float) -> float:
            return max(ymin, min(ymax, y))

        x1, x2 = xmin, xmax
        y1, y2 = clamp_y(slope * x1 + intercept), clamp_y(slope * x2 + intercept)
        parts.append(
            f"<line x1='{px(x1):.1f}' y1='{py(y1):.1f}' x2='{px(x2):.1f}' "
            f"y2='{py(y2):.1f}' stroke='var(--good)' stroke-width='2.5' />"
        )

    def tooltip_x(cx: float) -> float:
        # Keep the tooltip inside the SVG viewport.
        return max(left + 4, min(cx - 78, right - 176))

    def tooltip_y(cy: float) -> float:
        return cy + 12 if cy < top + 72 else cy - 70

    # Scatter dots (drawn last so they sit on top of the line).
    for p in points:
        cx, cy = px(float(p["score"])), py(float(p["actual_enrollment"]))
        label = str(p.get("course_label") or p.get("course_type") or p.get("label") or "")
        city_loc = " — ".join(
            str(v) for v in (p.get("city"), p.get("location")) if v
        )
        title = (
            f"{p.get('label') or p.get('course_label') or p.get('course_type')} | "
            f"Score {p['score']} | Avg enrollment {p['actual_enrollment']} | "
            f"Classes {p.get('historical_class_count') or 'unknown'} | "
            f"Basis {p.get('enrollment_basis') or 'unknown'}"
        )
        tx, ty = tooltip_x(cx), tooltip_y(cy)
        parts.append(
            "<g class='reg-point' tabindex='0'>"
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='6' fill='var(--accent)' "
            f"fill-opacity='0.85' stroke='#fff' stroke-width='1.5'>"
            f"<title>{_esc(title)}</title></circle>"
            f"<g class='reg-tooltip' transform='translate({tx:.1f} {ty:.1f})'>"
            "<rect width='172' height='58' rx='6' fill='#16242b' "
            "fill-opacity='0.96' stroke='#fff' stroke-width='1' />"
            f"<text x='8' y='15' font-size='11' font-weight='700' fill='#fff'>"
            f"{_esc(label[:28])}</text>"
            f"<text x='8' y='30' font-size='10' fill='#dbe7e5'>"
            f"Score {p['score']} · Avg {p['actual_enrollment']} · "
            f"Classes {p.get('historical_class_count') or 'unknown'}</text>"
            f"<text x='8' y='45' font-size='10' fill='#dbe7e5'>"
            f"{_esc(city_loc[:31])}</text>"
            "</g></g>"
        )

    # Axis titles.
    parts.append(
        f"<text x='{(left + right) / 2:.0f}' y='{bottom + 32:.0f}' "
        f"text-anchor='middle' font-size='11' fill='var(--ink)'>"
        f"{_esc(rv.get('x_label') or 'Opportunity score')}</text>"
    )
    parts.append(
        f"<text x='14' y='{(top + bottom) / 2:.0f}' text-anchor='middle' "
        f"font-size='11' fill='var(--ink)' transform='rotate(-90 14 "
        f"{(top + bottom) / 2:.0f})'>"
        f"{_esc(rv.get('y_label') or 'Actual historical enrollment')}</text>"
    )

    return (
        "<svg class=\"reg-svg\" viewBox=\"0 0 560 312\" "
        "xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        "aria-label=\"Opportunity score versus actual historical enrollment\">"
        "<style>.reg-tooltip{display:none;pointer-events:none}.reg-point:hover "
        ".reg-tooltip,.reg-point:focus .reg-tooltip{display:block}"
        ".reg-point:focus circle{stroke:#16242b;stroke-width:2}</style>"
        + "".join(parts) + "</svg>"
    )


def _validation_points_table(rv: Dict[str, Any]) -> str:
    points = [p for p in (rv.get("points") or [])
              if isinstance(p.get("score"), (int, float))
              and isinstance(p.get("actual_enrollment"), (int, float))]
    if not points:
        return ""
    rows = []
    for idx, p in enumerate(points, start=1):
        note = p.get("enrollment_basis") or ""
        if p.get("label"):
            note = (note + " · " if note else "") + str(p["label"])
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{_esc(p.get('city'))}</td>"
            f"<td>{_esc(p.get('location'))}</td>"
            f"<td>{_esc(p.get('course_label') or p.get('course_type'))}</td>"
            f"<td>{_esc(p.get('score'))}</td>"
            f"<td>{_esc(p.get('actual_enrollment'))}</td>"
            f"<td>{_esc(p.get('historical_class_count') or '')}</td>"
            f"<td>{_esc(note)}</td>"
            "</tr>"
        )
    return (
        "<h3>Validation points</h3>"
        "<table class=\"validation-points\"><thead><tr><th>#</th><th>City</th>"
        "<th>Location</th><th>Course type</th><th>Opportunity score</th>"
        "<th>Actual historical enrollment</th><th>Historical class count</th>"
        "<th>Note</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _regression_validation_html(rv: Optional[Dict[str, Any]]) -> str:
    """Render the 'Score vs Actual Enrollment Validation' section.

    A scatter of opportunity score (x) vs actual historical enrollment (y) with
    a least-squares line when ``n >= 3``, the regression read-out (slope,
    intercept, R², Pearson, Spearman), and an explicit honesty note. Presented
    as validation only — never a future guarantee.
    """
    if not rv:
        return ""

    enough = bool(rv.get("enough_data"))
    stats = (
        "<div class=\"reg-stats\">"
        + _reg_stat("Points (n)", rv.get("n"), kind="int")
        + _reg_stat("R²", rv.get("r_squared"))
        + _reg_stat("Pearson", rv.get("pearson"), kind="signed")
        + _reg_stat("Spearman", rv.get("spearman"), kind="signed")
        + _reg_stat("Slope", rv.get("slope"), kind="signed")
        + _reg_stat("Intercept", rv.get("intercept"), kind="signed")
        + "</div>"
    ) if enough else ""

    if enough:
        body = _regression_scatter_svg(rv) + stats + _validation_points_table(rv)
    else:
        scatter = _regression_scatter_svg(rv)  # still plot the few points we have
        warn = _esc(rv.get("warning") or
                    "Not enough historical outcome data for reliable regression.")
        body = (
            (scatter if scatter else "")
            + f"<p class=\"muted reg-warning\">⚠ {warn} "
            "No regression line drawn.</p>"
            + _validation_points_table(rv)
        )

    points = rv.get("points") or []
    cities = {str(p.get("city") or "").strip() for p in points}
    cities.discard("")
    area_txt = f"{next(iter(cities))} " if len(cities) == 1 else ""
    has_hybrid = any(p.get("course_type") == HYBRID_COURSE_KEY for p in points)
    cap = (
        f"Each dot = one {_esc(area_txt)}course type, not one candidate "
        "location. This validates course scoring against Enrollware history. "
        "Candidate locations are shown separately in the Center Opening "
        "Recommendation table and map. Line = least-squares fit (needs 3+ "
        "points)."
    )
    hybrid_cap = (
        f" <span class=\"muted\">{_esc(HYBRID_COURSE_NOTE)}</span>"
        if has_hybrid else ""
    )
    return (
        "<section class=\"summary-section reg-section\">"
        "<h2>Score vs Actual Enrollment Validation</h2>"
        "<p class=\"reg-badge\">Validation only — not a future guarantee.</p>"
        f"<p class=\"muted reg-cap\">{cap}</p>"
        + body
        + f"<p class=\"muted reg-note\">{_esc(rv.get('note') or '')}{hybrid_cap}</p>"
        + "</section>"
    )


_DECISION_CLASS = {
    "Open / Prioritize": "rd-strong",
    "Test first": "rd-moderate",
    "Keep watching": "tier-c",
    "Avoid for now": "rd-weak",
}


def _center_opening_html(co: Optional[Dict[str, Any]]) -> str:
    """Compact center-opening decision table — one row per course.

    Thin view over the evaluation graph's scores/confidence; no new scoring,
    no new styling beyond existing badges/tables.
    """
    recs = (co or {}).get("recommendations") or []
    if not recs:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><strong>{_esc(r.get('label'))}</strong></td>"
        f"<td>{_esc(r.get('opportunity_score'))}</td>"
        f"<td>{_esc(str(r.get('confidence', '')).replace('_', '-'))}</td>"
        f"<td><span class=\"badge {_DECISION_CLASS.get(r.get('decision'), 'tier-f')}\">"
        f"{_esc(r.get('decision'))}</span></td>"
        f"<td>{_esc(r.get('next_action'))}</td>"
        "</tr>"
        for r in recs
    )
    return (
        "<section class=\"summary-section\"><h2>Center-Opening Decisions</h2>"
        "<table><thead><tr><th>Course</th><th>Score</th><th>Confidence</th>"
        "<th>Decision</th><th>Next action</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class=\"muted reg-note\">{_esc((co or {}).get('honesty_note') or '')}</p>"
        "</section>"
    )


def _format_as_of_display_html(as_of_iso: str) -> str:
    if not as_of_iso:
        return "unknown"
    try:
        from datetime import datetime, timezone
        ts = as_of_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return as_of_iso


def _compact_source_audit(candidates: List[Dict[str, Any]],
                          report_style: str,
                          session_as_of: Optional[Dict[str, str]] = None) -> str:
    rows = build_compact_source_audit_for_candidates(
        candidates, session_as_of=session_as_of)
    if rows:
        body = "".join(
            "<tr>"
            f"<td>{_esc(r['source'])}</td>"
            f"<td>{_esc(r['detail'])}</td>"
            f"<td>{_esc(r['quality'])}</td>"
            f"<td>{_esc(_format_as_of_display_html(r.get('data_as_of', '')))}</td>"
            f"<td>{_esc(r['notes'])}</td>"
            "</tr>"
            for r in rows
        )
        compact = (
            "<table><thead><tr><th>Source</th><th>Records / fields</th>"
            "<th>Quality</th><th>As of</th><th>Notes</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )
    else:
        compact = "<p class=\"muted\">No source audit records available.</p>"

    # Full per-field audit — collapsible.
    detail_rows = []
    for item in candidates:
        profile = item.get("profile") or {}
        candidate = profile.get("candidate_name") or profile.get("candidate_id") \
            or "unknown"
        for row in build_source_audit_rows(profile.get("sources") or []):
            fields = ", ".join(row.get("fields_populated") or []) or "unknown"
            detail_rows.append(
                "<tr>"
                f"<td>{_esc(candidate)}</td>"
                f"<td>{_esc(row.get('source_name'))}</td>"
                f"<td>{_esc(_safe_url(row.get('source_api_or_url')) or 'unknown')}</td>"
                f"<td>{_esc(row.get('retrieved_at'))}</td>"
                f"<td>{_esc(row.get('source_quality'))}</td>"
                f"<td>{_esc(fields)}</td>"
                f"<td>{_esc(row.get('confidence'))}</td>"
                "</tr>"
            )
    detail_open = " open" if report_style == "debug" else ""
    detail = (
        f"<details{detail_open}><summary>Detailed per-field source audit "
        f"appendix ({len(detail_rows)} rows)</summary>"
        "<table><thead><tr><th>Candidate</th><th>Source name</th>"
        "<th>Source API / URL</th><th>Retrieved at</th><th>Quality</th>"
        "<th>Fields populated</th><th>Confidence</th></tr></thead>"
        f"<tbody>{''.join(detail_rows)}</tbody></table></details>"
        if detail_rows else ""
    )
    return (
        "<section class=\"summary-section\"><h2>Source audit</h2>"
        f"{compact}{detail}</section>"
    )


# --------------------------------------------------------------------------- #
# Report-wide ZIP demand visualization (table + scatter + centroid map)
# --------------------------------------------------------------------------- #
#
# Reads one precomputed dataset (context["zip_demand_report"], built by
# app.scoring.zip_demand.build_zip_demand_report). All three views share that
# dataset, so the table, the chart, and the map can never disagree about a
# ZIP's score, class count, or centroid status.

# Demand-score color bands — reused by the scatter and the centroid map so a
# score reads the same color in both. Mirrors demand_strength_category cuts.
def _zip_score_color(score: Any) -> str:
    if not isinstance(score, (int, float)):
        return "var(--muted)"
    if score >= 75:
        return "var(--good)"
    if score >= 60:
        return "var(--accent)"
    if score >= 40:
        return "var(--mid)"
    return "var(--low)"


def _zip_report_table(rows: List[Dict[str, Any]]) -> str:
    """Every demand ZIP with score, raw class history, and centroid status."""
    body = []
    for r in rows:
        present = r.get("centroid_present")
        cen = ("<span class=\"badge zip-ok\">covered</span>" if present
               else "<span class=\"badge zip-missing\">missing</span>")
        lat, lng = r.get("lat"), r.get("lng")
        coords = (f"{lat:.4f}, {lng:.4f}"
                  if isinstance(lat, (int, float))
                  and isinstance(lng, (int, float)) else "—")
        fill = r.get("fill_rate")
        fill_text = (f"{fill:.1f}%" if isinstance(fill, (int, float)) else "—")
        conf = r.get("confidence_modifier")
        conf_text = (f"{conf:+.1f}" if isinstance(conf, (int, float)) else "—")
        dot = (f"<span class=\"zip-dot\" style=\"background:"
               f"{_zip_score_color(r.get('demand_score'))}\"></span>")
        body.append(
            "<tr>"
            f"<td>{dot}{_esc(r.get('zip'))}</td>"
            f"<td>{_score(r.get('demand_score'))}</td>"
            f"<td>{_esc(r.get('strength'))}</td>"
            f"<td>{_esc(r.get('classes'))}</td>"
            f"<td>{_score(r.get('avg_students'))}</td>"
            f"<td>{_esc(fill_text)}</td>"
            f"<td>{_esc(conf_text)}</td>"
            f"<td>{cen}</td>"
            f"<td>{_esc(coords)}</td>"
            "</tr>"
        )
    return (
        "<table class=\"zip-report-table\"><thead><tr>"
        "<th>ZIP</th><th>Demand score</th><th>Strength</th><th>Classes</th>"
        "<th>Avg students</th><th>Fill rate</th><th>Confidence ±</th>"
        "<th>Centroid</th><th>Lat / Lng</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _zip_scatter_svg(points: List[Dict[str, Any]], series: Dict[str, Any],
                     x_label: str = "ZIP demand score") -> str:
    """One validation scatter: ZIP demand score (x) vs the series' actual demand
    outcome (y), with a least-squares trend line. One dot per ZIP — a zero
    outcome plots at y=0, never dropped. ``series`` is one entry of the
    ``charts.series`` list (its ``key`` selects the y field in each point, and
    it carries the fit). Dependency-free SVG — no JS, survives offline."""
    key = series.get("key")
    pts = [p for p in (points or [])
           if isinstance(p.get(key), (int, float))
           and isinstance(p.get("demand_score"), (int, float))
           and (not series.get("hide_zero_y") or float(p[key]) > 0.0)]
    if not pts:
        return ""

    left, right, top, bottom = 58.0, 540.0, 18.0, 268.0
    plot_w, plot_h = right - left, bottom - top
    xmin, xmax = 0.0, 100.0    # demand score is a fixed 0..100 scale
    ys = [float(p[key]) for p in pts]
    ymin = 0.0
    ymax = max(ys) * 1.1 if max(ys) > 0 else 1.0

    def px(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * plot_w

    def py(y: float) -> float:
        return bottom - (y - ymin) / (ymax - ymin) * plot_h

    parts = [
        f"<rect x='{left:.0f}' y='{top:.0f}' width='{plot_w:.0f}' "
        f"height='{plot_h:.0f}' fill='#fff' stroke='var(--line)' />"
    ]
    # Horizontal gridlines + y labels (outcome axis, variable scale).
    for i in range(1, 5):
        gy = top + plot_h * i / 5.0
        val = ymax - (ymax - ymin) * i / 5.0
        parts.append(
            f"<line x1='{left:.0f}' y1='{gy:.1f}' x2='{right:.0f}' y2='{gy:.1f}' "
            f"stroke='var(--line)' stroke-dasharray='3 3' />"
        )
        parts.append(
            f"<text x='{left - 6:.0f}' y='{gy + 3:.1f}' text-anchor='end' "
            f"font-size='10' fill='var(--muted)'>{val:.0f}</text>"
        )
    parts.append(
        f"<text x='{left - 6:.0f}' y='{top + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>{ymax:.0f}</text>")
    parts.append(
        f"<text x='{left - 6:.0f}' y='{bottom + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>0</text>")
    # x axis labels at 0 / 50 / 100 (score).
    for xv in (0, 50, 100):
        anchor = "start" if xv == 0 else ("end" if xv == 100 else "middle")
        parts.append(
            f"<text x='{px(xv):.0f}' y='{bottom + 16:.0f}' text-anchor='{anchor}' "
            f"font-size='10' fill='var(--muted)'>{xv}</text>")

    if series.get("enough_data") and series.get("slope") is not None:
        slope = float(series["slope"])
        intercept = float(series["intercept"])
        # Draw the TRUE straight OLS line, clipped to the plot rectangle. We
        # clip the x-interval to where y stays in [ymin, ymax] rather than
        # clamping each endpoint's y independently — independent clamping bends
        # the segment and misrepresents the slope (e.g. an intercept of -100
        # snapped to 0). Here the visible slope always equals the fitted slope.
        lo, hi = xmin, xmax
        if abs(slope) > 1e-12:
            xa = (ymin - intercept) / slope
            xb = (ymax - intercept) / slope
            lo = max(lo, min(xa, xb))
            hi = min(hi, max(xa, xb))
        if hi > lo:   # some of the line is visible
            y1, y2 = slope * lo + intercept, slope * hi + intercept
            parts.append(
                f"<line x1='{px(lo):.1f}' y1='{py(y1):.1f}' x2='{px(hi):.1f}' "
                f"y2='{py(y2):.1f}' stroke='var(--good)' stroke-width='2.5' />"
            )

    def tip_x(cx: float) -> float:
        return max(left + 4, min(cx - 110, right - 236))

    def tip_y(cy: float) -> float:
        return cy + 12 if cy < top + 140 else cy - 138

    def _n(v, suffix=""):
        return (f"{v:g}{suffix}" if isinstance(v, (int, float)) else "—")

    dot_parts: List[str] = []
    tooltip_parts: List[str] = []
    for p in pts:
        cx, cy = px(float(p["demand_score"])), py(float(p[key]))
        y_metric_t = _n(p.get(key))
        y_metric_label = str(series.get("y_label") or "Y value")
        avg_t = _n(p.get("avg_students"))
        fill_t = _n(p.get("fill_rate"), "%")
        students_month_t = _n(p.get("students_per_month"))
        classes_month_t = _n(p.get("held_classes_per_month"))
        cpr_month_t = _n(p.get("arc_cpr_students_per_month"))
        bls_month_t = _n(p.get("arc_bls_students_per_month"))
        aha_month_t = _n(p.get("aha_bls_students_per_month"))
        conf = p.get("confidence_modifier")
        conf_t = f"{conf:+.1f}" if isinstance(conf, (int, float)) else "—"
        cpr, bls, aha = (p.get("arc_cpr_students"), p.get("arc_bls_students"),
                         p.get("aha_bls_students"))
        title = (
            f"ZIP {p['zip']} | Score {p['demand_score']} | "
            f"{y_metric_label} {y_metric_t} | "
            f"Total classes {p['classes']} | Students/month {students_month_t} | "
            f"Classes/month {classes_month_t} | Overall avg students/class {avg_t} | "
            f"Fill {fill_t} | ARC CPR/month {cpr_month_t} | "
            f"ARC BLS/month {bls_month_t} | AHA BLS/month {aha_month_t} | "
            f"Confidence {conf_t} | Raw students: ARC CPR {_n(cpr)}, "
            f"ARC BLS {_n(bls)}, AHA BLS {_n(aha)}")
        tx, ty = tip_x(cx), tip_y(cy)
        dot_parts.append(
            "<g class='reg-point' tabindex='0'>"
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='5.5' "
            f"fill='{_zip_score_color(p['demand_score'])}' fill-opacity='0.85' "
            f"stroke='#fff' stroke-width='1.4'><title>{_esc(title)}</title></circle>"
            "</g>"
        )
        tooltip_parts.append(
            "<g class='reg-hit' tabindex='0'>"
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='8' fill='transparent' "
            "stroke='transparent' />"
            f"<g class='reg-tooltip' transform='translate({tx:.1f} {ty:.1f})'>"
            "<rect width='232' height='130' rx='6' fill='#16242b' "
            "fill-opacity='0.96' stroke='#fff' stroke-width='1' />"
            f"<text x='8' y='15' font-size='11' font-weight='700' fill='#fff'>"
            f"ZIP {_esc(p['zip'])}</text>"
            f"<text x='8' y='30' font-size='10' fill='#dbe7e5'>"
            f"Score {p['demand_score']} · {p['classes']} total classes</text>"
            f"<text x='8' y='44' font-size='10' fill='#dbe7e5'>"
            f"Y: {_esc(y_metric_label)} {_esc(y_metric_t)}</text>"
            f"<text x='8' y='58' font-size='10' fill='#dbe7e5'>"
            f"Students/mo {_esc(students_month_t)} · Classes/mo "
            f"{_esc(classes_month_t)}</text>"
            f"<text x='8' y='72' font-size='10' fill='#dbe7e5'>"
            f"Overall avg students/class {_esc(avg_t)} · Fill {_esc(fill_t)}</text>"
            f"<text x='8' y='86' font-size='10' fill='#dbe7e5'>"
            f"ARC CPR/mo {_esc(cpr_month_t)} · ARC BLS/mo "
            f"{_esc(bls_month_t)}</text>"
            f"<text x='8' y='100' font-size='10' fill='#dbe7e5'>"
            f"AHA BLS/mo {_esc(aha_month_t)}</text>"
            f"<text x='8' y='114' font-size='10' fill='#dbe7e5'>"
            f"Confidence {_esc(conf_t)}</text>"
            "</g></g>"
        )
    parts.extend(dot_parts)
    parts.extend(tooltip_parts)

    parts.append(
        f"<text x='{(left + right) / 2:.0f}' y='{bottom + 32:.0f}' "
        f"text-anchor='middle' font-size='11' fill='var(--ink)'>"
        f"{_esc(x_label)}</text>")
    parts.append(
        f"<text x='14' y='{(top + bottom) / 2:.0f}' text-anchor='middle' "
        f"font-size='11' fill='var(--ink)' transform='rotate(-90 14 "
        f"{(top + bottom) / 2:.0f})'>"
        f"{_esc(series.get('y_label') or 'Actual demand outcome')}</text>")

    return (
        "<svg class=\"reg-svg\" viewBox=\"0 0 560 312\" "
        "xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        f"aria-label=\"{_esc(series.get('title') or 'ZIP demand validation')}\">"
        "<style>.reg-tooltip{display:none;pointer-events:none}.reg-hit:hover "
        ".reg-tooltip,.reg-hit:focus .reg-tooltip{display:block}"
        ".reg-hit:focus circle{stroke:#16242b;stroke-width:2}</style>"
        + "".join(parts) + "</svg>"
    )


def _zip_chart_stats(s: Dict[str, Any]) -> str:
    """The R²/slope/sample read-out (or a not-enough-data badge) for one fit."""
    hidden = int(s.get("hidden_zero_y") or 0)
    hidden_note = (
        f"<p class=\"reg-cap muted\">{hidden} zero-value ZIP(s) hidden from "
        "this avg/class chart.</p>"
        if hidden else ""
    )
    if s.get("enough_data"):
        return (
            "<div class=\"reg-stats\">"
            + _reg_stat("R²", s.get("r_squared"))
            + _reg_stat("Slope", s.get("slope"), kind="signed")
            + _reg_stat("Sample (ZIPs)", s.get("n"), kind="int")
            + "</div>"
            + hidden_note
        )
    return ("<p class=\"reg-badge\">Not enough ZIPs (need ≥3 with varying "
            "scores) to fit a trend line.</p>" + hidden_note)


def _zip_demand_charts_html(charts: Dict[str, Any]) -> str:
    """The four score→outcome validation scatters in a responsive grid, each
    with its own title, chart, and R²/slope/sample read-out."""
    points = charts.get("points") or []
    series = charts.get("series") or []
    x_label = charts.get("x_label") or "ZIP demand score"
    if not points or not series:
        return "<p class=\"muted\">No plottable ZIPs.</p>"

    cards = []
    for s in series:
        cards.append(
            "<div class=\"zip-chart-card\">"
            f"<h4>{_esc(s.get('title'))}</h4>"
            + _zip_scatter_svg(points, s, x_label)
            + _zip_chart_stats(s) + "</div>"
        )
    return "<div class=\"zip-charts-grid\">" + "".join(cards) + "</div>"


def _zip_combined_chart_html(charts: Dict[str, Any]) -> str:
    """The final boss-facing opportunity chart: ZIP demand score (x) vs the
    normalized historical demand score (y). Full width, with a
    quadrant guide so the read is unambiguous."""
    points = charts.get("points") or []
    combined = charts.get("combined") or {}
    x_label = charts.get("x_label") or "ZIP demand score"
    if not points or not combined.get("key"):
        return ""

    guide = (
        "<ul class=\"zip-quadrant\">"
        "<li><strong>Top-right</strong> — high score &amp; strong history: "
        "best location opportunity.</li>"
        "<li><strong>Bottom-right</strong> — score high but history weak: "
        "model optimistic; verify before committing.</li>"
        "<li><strong>Top-left</strong> — history strong but score low: scoring "
        "may be undervaluing the ZIP; worth a look.</li>"
        "<li><strong>Bottom-left</strong> — weak on both: low priority.</li>"
        "</ul>"
    )
    note = (
        "<p class=\"reg-cap muted\">This historical demand score balances "
        "historical volume with class efficiency, so ZIPs with many low-filled "
        "classes do not "
        "automatically outrank ZIPs with fewer but stronger classes. Y averages "
        "seven 0–100 normalized metrics: students/month, classes/month, average "
        "students/class, fill rate, ARC CPR students/month, ARC BLS "
        "students/month, and AHA BLS students/month. Normalization is capped at "
        "a high-percentile benchmark so one extreme ZIP does not dominate the "
        "scale. A ZIP that is zero in a metric keeps that zero.</p>"
    )
    return (
        "<div class=\"zip-chart-card zip-chart-wide\">"
        f"<h4>{_esc(combined.get('title'))}</h4>"
        + note
        + _zip_scatter_svg(points, combined, x_label)
        + _zip_chart_stats(combined)
        + guide
        + "</div>"
    )


def _zip_ranking_svg(rows: List[Dict[str, Any]]) -> str:
    """Vertical bar chart of every ZIP's demand score, ranked high→low.
    X = ZIP rank (1 = highest), Y = demand score (0..100). Answers "what score
    did each ZIP get?" at a glance. Bars colored by the score band; hover for
    the ZIP and score. Dependency-free SVG."""
    pts = [r for r in rows if isinstance(r.get("demand_score"), (int, float))]
    if not pts:
        return ""
    pts = sorted(pts, key=lambda r: -float(r["demand_score"]))

    left, right, top, bottom = 40.0, 552.0, 14.0, 250.0
    plot_w, plot_h = right - left, bottom - top
    n = len(pts)
    # Bars share the width with a small gap; thin bars on big national runs.
    slot = plot_w / n
    bar_w = max(1.5, slot * 0.78)

    def py(score: float) -> float:
        return bottom - (score / 100.0) * plot_h

    parts = [
        f"<rect x='{left:.0f}' y='{top:.0f}' width='{plot_w:.0f}' "
        f"height='{plot_h:.0f}' fill='#fff' stroke='var(--line)' />"
    ]
    for i in range(1, 5):
        gy = top + plot_h * i / 5.0
        val = 100.0 - 100.0 * i / 5.0
        parts.append(
            f"<line x1='{left:.0f}' y1='{gy:.1f}' x2='{right:.0f}' y2='{gy:.1f}' "
            f"stroke='var(--line)' stroke-dasharray='3 3' />")
        parts.append(
            f"<text x='{left - 6:.0f}' y='{gy + 3:.1f}' text-anchor='end' "
            f"font-size='10' fill='var(--muted)'>{val:.0f}</text>")
    parts.append(
        f"<text x='{left - 6:.0f}' y='{top + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>100</text>")
    parts.append(
        f"<text x='{left - 6:.0f}' y='{bottom + 3:.0f}' text-anchor='end' "
        f"font-size='10' fill='var(--muted)'>0</text>")

    # Label ranks sparsely so the axis stays readable on large runs.
    label_every = max(1, n // 12)
    for idx, r in enumerate(pts):
        rank = idx + 1
        score = float(r["demand_score"])
        x = left + idx * slot + (slot - bar_w) / 2.0
        y = py(score)
        h = bottom - y
        classes = r.get("classes")
        title = f"#{rank} ZIP {r['zip']} | Score {r['demand_score']} | {classes} classes"
        parts.append(
            f"<rect x='{x:.2f}' y='{y:.2f}' width='{bar_w:.2f}' height='{h:.2f}' "
            f"fill='{_zip_score_color(score)}' fill-opacity='0.9'>"
            f"<title>{_esc(title)}</title></rect>")
        if (rank - 1) % label_every == 0:
            parts.append(
                f"<text x='{x + bar_w / 2:.2f}' y='{bottom + 14:.0f}' "
                f"text-anchor='middle' font-size='9' fill='var(--muted)'>"
                f"{rank}</text>")

    parts.append(
        f"<text x='{(left + right) / 2:.0f}' y='{bottom + 30:.0f}' "
        f"text-anchor='middle' font-size='11' fill='var(--ink)'>"
        f"ZIP rank (1 = highest demand score) — hover a bar for its ZIP</text>")
    parts.append(
        f"<text x='12' y='{(top + bottom) / 2:.0f}' text-anchor='middle' "
        f"font-size='11' fill='var(--ink)' transform='rotate(-90 12 "
        f"{(top + bottom) / 2:.0f})'>ZIP demand score</text>")

    return (
        "<svg class=\"reg-svg\" viewBox=\"0 0 564 282\" "
        "xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        "aria-label=\"ZIP demand score by rank\">" + "".join(parts) + "</svg>"
    )


def _zip_centroid_map_svg(rows: List[Dict[str, Any]]) -> str:
    """Equirectangular scatter of every covered ZIP centroid, colored and sized
    by demand score. Dependency-free SVG (no tiles) so it prints and survives
    offline. ZIPs without a centroid are listed separately by the caller."""
    pts = [r for r in rows
           if isinstance(r.get("lat"), (int, float))
           and isinstance(r.get("lng"), (int, float))]
    if not pts:
        return ""

    left, right, top, bottom = 20.0, 540.0, 18.0, 300.0
    plot_w, plot_h = right - left, bottom - top
    lats = [float(r["lat"]) for r in pts]
    lngs = [float(r["lng"]) for r in pts]
    lat_min, lat_max = min(lats), max(lats)
    lng_min, lng_max = min(lngs), max(lngs)
    # Pad so edge dots aren't clipped; expand a zero-spread axis to a degree.
    lat_pad = (lat_max - lat_min) * 0.12 or 0.05
    lng_pad = (lng_max - lng_min) * 0.12 or 0.05
    lat_min, lat_max = lat_min - lat_pad, lat_max + lat_pad
    lng_min, lng_max = lng_min - lng_pad, lng_max + lng_pad

    def px(lng: float) -> float:
        return left + (lng - lng_min) / (lng_max - lng_min) * plot_w

    def py(lat: float) -> float:
        # North up: larger latitude -> smaller y.
        return top + (lat_max - lat) / (lat_max - lat_min) * plot_h

    parts = [
        f"<rect x='{left:.0f}' y='{top:.0f}' width='{plot_w:.0f}' "
        f"height='{plot_h:.0f}' fill='#f5f8f7' stroke='var(--line)' />"
    ]
    smax = max((r["demand_score"] for r in pts
                if isinstance(r.get("demand_score"), (int, float))), default=0) or 1
    # Label every dot only on small regional runs; a national run (dozens of
    # ZIPs) turns labels into noise, so there we lean on the hover tooltip.
    show_labels = len(pts) <= 25
    for r in pts:
        cx, cy = px(float(r["lng"])), py(float(r["lat"]))
        score = r.get("demand_score")
        radius = (5.0 + 7.0 * (float(score) / float(smax))
                  if isinstance(score, (int, float)) else 4.0)
        avg = r.get("avg_students")
        avg_t = f"{avg:.1f}" if isinstance(avg, (int, float)) else "—"
        fill = r.get("fill_rate")
        fill_t = f"{fill:.1f}%" if isinstance(fill, (int, float)) else "—"
        title = (f"ZIP {r['zip']} | Demand {_score(score)} | "
                 f"Classes {r['classes']} | Avg {avg_t} | Fill {fill_t}")
        parts.append(
            f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='{radius:.1f}' "
            f"fill='{_zip_score_color(score)}' fill-opacity='0.78' "
            f"stroke='#fff' stroke-width='1.2'><title>{_esc(title)}</title>"
            "</circle>")
        if show_labels:
            parts.append(
                f"<text x='{cx:.1f}' y='{cy - radius - 2:.1f}' "
                f"text-anchor='middle' font-size='9' fill='var(--ink)'>"
                f"{_esc(r['zip'])}</text>")
    # Corner lat/lng labels for orientation.
    parts.append(
        f"<text x='{left + 2:.0f}' y='{bottom - 4:.0f}' font-size='9' "
        f"fill='var(--muted)'>{lat_min:.2f}, {lng_min:.2f}</text>")
    parts.append(
        f"<text x='{right - 2:.0f}' y='{top + 11:.0f}' text-anchor='end' "
        f"font-size='9' fill='var(--muted)'>{lat_max:.2f}, {lng_max:.2f}</text>")

    return (
        "<svg class=\"zip-map-svg\" viewBox=\"0 0 560 320\" "
        "xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" "
        "aria-label=\"ZIP centroid map colored by demand score\">"
        + "".join(parts) + "</svg>"
    )


def _zip_demand_report_section(context: Dict[str, Any]) -> str:
    """The boss-facing ZIP demand visualization: score ranking, full table, four
    score→outcome validation scatters, and centroid map. Empty string when the
    run carried no ZIP-resolved demand (so the caller can omit the section)."""
    data = (context or {}).get("zip_demand_report") or {}
    rows = data.get("rows") or []
    if not rows:
        return ""

    charts = data.get("charts") or {}
    coverage = data.get("coverage") or {}
    missing = data.get("missing_centroid_zips") or []
    cov_pct = coverage.get("coverage_pct")

    # Validation caption + an explicit note about which ZIPs are plotted.
    excluded = int((charts.get("zips_excluded_no_score") or 0)
                   + (charts.get("zips_excluded_no_valid_month_span") or 0))
    plotted = int(charts.get("zips_plotted") or 0)
    excluded_rows = charts.get("zips_excluded") or []
    excluded_reason_html = ""
    if excluded_rows:
        items = "".join(
            f"<li>{_esc(e.get('zip'))}: "
            f"{_esc(', '.join(e.get('reasons') or ['unknown reason']))}</li>"
            for e in excluded_rows)
        excluded_reason_html = (
            "<ul class=\"reg-cap muted zip-exclusions\">" + items + "</ul>")
    charts_note = (
        "<p class=\"reg-cap muted\">Each chart plots one dot per ZIP: x = ZIP "
        "demand score, y = average students per class for that historical "
        "slice. Zero-value ZIPs are hidden in these avg/class charts because "
        "they usually mean that course type did not run in that ZIP. "
        + (f"All {plotted} demand ZIP(s) are plotted."
           if excluded == 0 else
           f"{plotted} plotted; {excluded} excluded because they lacked a "
           "valid score or historical month span.")
        + " The score feeds these outcomes, so read R²/slope as a sanity check, "
        "not independent proof.</p>"
        + excluded_reason_html
    )

    cov_text = (f"{cov_pct:.1f}% of demand ZIPs have a centroid"
                if isinstance(cov_pct, (int, float))
                else "no centroid file loaded")
    missing_html = ""
    if missing:
        chips = "".join(
            f"<span class=\"badge zip-missing\">{_esc(z)}</span>" for z in missing)
        missing_html = (
            "<div class=\"zip-missing-list\">"
            f"<p class=\"muted\">{len(missing)} demand ZIP(s) have no centroid "
            "and are not plotted on the map — refresh "
            "<code>data/reference/zip_centroids.csv</code> via "
            "<code>scripts/build_zip_centroids.py</code> to include them:</p>"
            f"<p class=\"zip-chips\">{chips}</p></div>"
        )
    else:
        missing_html = ("<p class=\"muted\">Every demand ZIP has a centroid — "
                        "all are plotted.</p>")

    map_svg = _zip_centroid_map_svg(rows)
    map_block = (
        "<h3>ZIP centroid map</h3>"
        "<p class=\"muted\">Each covered ZIP plotted at its Census centroid; "
        "dot color and size scale with demand score. Hover a dot for its ZIP, "
        "score, and class stats.</p>"
        + (map_svg or "<p class=\"muted\">No covered ZIP has a centroid to "
                      "plot.</p>")
        + missing_html
    )

    return (
        "<section class=\"summary-section\"><h2>ZIP demand</h2>"
        f"<p class=\"muted\">{data.get('total_zips', len(rows))} demand ZIP(s), "
        f"{data.get('total_classes', 0)} held class(es); {_esc(cov_text)}.</p>"
        # 1. What score did each ZIP get? — ranking bars + the full table.
        "<h3>ZIP score ranking</h3>"
        "<p class=\"muted\">Every demand ZIP ranked by demand score, high to "
        "low. Hover a bar for its ZIP.</p>"
        + (_zip_ranking_svg(rows) or "")
        + "<h3>ZIP demand table</h3>"
        + _zip_report_table(rows)
        # 2. Does the score line up with class efficiency? — four score→average
        #    class-size scatters (ARC CPR / ARC BLS / AHA BLS, overall).
        + "<h3>Score vs actual demand (validation)</h3>"
        + charts_note
        + _zip_demand_charts_html(charts)
        # 3. One combined opportunity read: score vs the normalized blend of all
        #    four outcomes. Top-right = best location.
        + "<h3>Final ZIP opportunity</h3>"
        + _zip_combined_chart_html(charts)
        + map_block
        + "</section>"
    )


# --------------------------------------------------------------------------- #
# Public render
# --------------------------------------------------------------------------- #

def _resolve_style(context: Dict[str, Any],
                   report_style: Optional[str]) -> str:
    style = report_style or context.get("report_style") or "executive"
    return style if style in VALID_STYLES else "executive"


def render_html_report(payload: Dict[str, Any], top_n: Optional[int] = None,
                       title: str = "ALLCPR Site Intelligence Report",
                       report_style: Optional[str] = None) -> str:
    """Render a complete dashboard HTML report from the JSON report payload."""
    context = payload.get("context") or {}
    style = _resolve_style(context, report_style)
    candidates = list(payload.get("candidates") or [])
    if top_n is not None:
        candidates = candidates[:top_n]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = context.get("mode") or "unknown"
    cache_session_as_of = context.get("cache_session_as_of") or None

    # Report-level interpretation: prefer precomputed; else derive.
    report_interp = payload.get("report_interpretation")
    if not report_interp:
        ranked = [(c.get("profile") or {}, c.get("scored") or {})
                  for c in candidates]
        report_interp = build_report_interpretation(ranked)

    candidate_cards = "\n".join(
        _candidate_card(item, style, fallback_rank=index,
                        session_as_of=cache_session_as_of)
        for index, item in enumerate(candidates, start=1)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --ink: #16242b;
      --muted: #5f6c72;
      --line: #e2e7ea;
      --soft: #f5f8f7;
      --bg: #fbfcfc;
      --accent: #0f766e;
      --accent-soft: #e6f1ef;
      --good: #15803d;
      --mid: #b45309;
      --low: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0; color: var(--ink); background: var(--bg);
      font: 15px/1.62 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 16px 24px 64px; }}
    h1, h2, h3, h4 {{ margin: 0 0 8px; line-height: 1.25; }}
    h1 {{ font-size: 27px; letter-spacing: -.01em; }}
    h2 {{ font-size: 20px; letter-spacing: -.01em; padding-bottom: 6px;
          border-bottom: 2px solid var(--accent-soft); }}
    h3 {{ font-size: 12.5px; margin-top: 16px; text-transform: uppercase;
          letter-spacing: .05em; color: var(--muted); }}
    h4 {{ font-size: 13px; margin-top: 12px; }}
    p {{ margin: 0 0 8px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    /* Readable tables: zebra rows, light rules, sticky header, aligned nums */
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0 14px;
             font-size: 13px; background: #fff; border: 1px solid var(--line);
             border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 8px 10px; text-align: left; vertical-align: top;
              border-bottom: 1px solid var(--line); }}
    td:not(:first-child), th:not(:first-child) {{
      font-variant-numeric: tabular-nums; }}
    th {{ background: var(--soft); font-weight: 700; color: var(--ink);
          position: sticky; top: 0; }}
    tbody tr:nth-child(even) {{ background: #fafbfb; }}
    tbody tr:hover {{ background: var(--accent-soft); }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    ul, ol {{ margin: 6px 0 12px 18px; padding: 0; }}
    li {{ margin-bottom: 3px; }}
    .muted {{ color: var(--muted); }}
    .anchor {{ display: block; height: 0; scroll-margin-top: 60px; }}
    .report-header {{ border-bottom: 2px solid var(--ink);
                      padding-bottom: 14px; margin-bottom: 8px; }}
    /* Sticky table-of-contents nav */
    .toc {{ position: sticky; top: 0; z-index: 20; display: flex; flex-wrap: wrap;
            gap: 4px; padding: 8px 0; margin-bottom: 16px;
            background: rgba(251,252,252,.92); backdrop-filter: blur(6px);
            border-bottom: 1px solid var(--line); }}
    .toc a {{ font-size: 12.5px; font-weight: 600; color: var(--muted);
              padding: 5px 11px; border-radius: 999px; }}
    .toc a:hover {{ background: var(--accent-soft); color: var(--accent);
                    text-decoration: none; }}
    .accuracy {{ background: var(--accent-soft); border-left: 4px solid var(--accent);
                 padding: 10px 14px; margin: 12px 0; font-size: 13px;
                 border-radius: 0 8px 8px 0; }}
    /* Executive summary panel (scrolls with the page) */
    .exec-panel {{ background: #fff;
                   border: 1px solid var(--accent); border-top: 4px solid var(--accent);
                   border-radius: 12px; padding: 18px 20px; margin: 16px 0 24px;
                   box-shadow: 0 1px 3px rgba(16,24,30,.05); }}
    .exec-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
                  margin: 8px 0; }}
    .exec-row {{ background: var(--soft); border-radius: 8px; padding: 9px 12px; }}
    .exec-row span {{ display: block; color: var(--muted); font-size: 11px;
                      text-transform: uppercase; letter-spacing: .04em;
                      margin-bottom: 2px; }}
    .next-actions li {{ margin-bottom: 4px; }}
    /* Content sections render as clean cards */
    .summary-section {{ margin: 18px 0; background: #fff; border: 1px solid var(--line);
                        border-radius: 12px; padding: 18px 20px;
                        box-shadow: 0 1px 3px rgba(16,24,30,.04); }}
    .summary-section.flush {{ background: none; border: 0; box-shadow: none;
                              padding: 0; }}
    /* Candidate cards */
    .candidate-card {{ border: 1px solid var(--line); border-radius: 12px;
                       padding: 18px 20px; margin: 16px 0; break-inside: avoid;
                       page-break-inside: avoid; background: #fff;
                       box-shadow: 0 1px 3px rgba(16,24,30,.04); }}
    .candidate-card header {{ display: flex; justify-content: space-between;
                              gap: 16px; border-bottom: 1px solid var(--line);
                              padding-bottom: 12px; margin-bottom: 8px; }}
    .eyebrow {{ color: var(--accent); font-weight: 700; font-size: 11px;
                text-transform: uppercase; letter-spacing: .05em; }}
    .addr {{ color: var(--muted); }}
    .anchor-block {{ display: grid; grid-template-columns: repeat(2, 1fr);
                     gap: 8px; margin: 6px 0 10px; }}
    .anchor-block > div {{ background: var(--soft); border-radius: 8px;
                           padding: 8px 10px; }}
    .anchor-block span {{ display: block; color: var(--muted); font-size: 11px;
                          text-transform: uppercase; letter-spacing: .03em; }}
    .anchor-block span.muted {{ display: inline; text-transform: none;
                                letter-spacing: 0; font-size: 11px; }}
    .anchor-block strong {{ font-size: 14px; }}
    .proxy-validate {{ border-left: 3px solid var(--mid); background: #fffaf2;
                       padding: 8px 12px; margin: 8px 0 0; border-radius: 6px; }}
    .badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .badge {{ display: inline-block; padding: 3px 9px; border-radius: 999px;
              font-size: 12px; font-weight: 700; color: #fff; }}
    .tier-a {{ background: var(--good); }}
    .tier-b {{ background: var(--accent); }}
    .tier-c {{ background: var(--mid); }}
    .tier-d {{ background: #c2410c; }}
    .tier-f {{ background: var(--low); }}
    .rd-strong {{ background: var(--good); }}
    .rd-moderate {{ background: var(--mid); }}
    .rd-weak {{ background: var(--low); }}
    /* Course Opportunity Graph — stacked contribution chart */
    .eval-chart {{ margin: 10px 0 6px; display: flex; flex-direction: column;
                   gap: 10px; }}
    .eval-row {{ display: grid; grid-template-columns: 180px 1fr 38px;
                 align-items: center; gap: 10px; }}
    .eval-row-label {{ display: flex; flex-direction: column; gap: 3px;
                       align-items: flex-start; font-size: 13px; }}
    .eval-row-score {{ font-weight: 800; font-size: 15px; text-align: right; }}
    .eval-stack {{ display: flex; height: 18px; border-radius: 6px;
                   overflow: hidden; background: #eef2f6;
                   box-shadow: inset 0 0 0 1px var(--line); }}
    .eval-seg {{ height: 100%; transition: width .2s; }}
    .eval-seg-penalty {{ background-image: repeating-linear-gradient(45deg,
                         #b4453a 0, #b4453a 4px, #d98b84 4px, #d98b84 8px); }}
    .eval-legend {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0 2px;
                    font-size: 11px; color: var(--muted); }}
    .eval-legend-item {{ display: inline-flex; align-items: center; gap: 5px; }}
    .eval-swatch {{ width: 11px; height: 11px; border-radius: 3px;
                    display: inline-block; }}
    .eval-penalty-swatch {{ background-image: repeating-linear-gradient(45deg,
                            #b4453a 0, #b4453a 3px, #d98b84 3px, #d98b84 6px); }}
    .eval-cap {{ margin: 0 0 12px; }}
    /* Headline recommendation strip */
    .eval-rec {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
                 margin: 4px 0 14px; }}
    .eval-rec-cell {{ border-radius: 8px; padding: 9px 11px; border: 1px solid
                      var(--line); }}
    .eval-rec-cell > span {{ display: block; font-size: 10px; font-weight: 700;
                             text-transform: uppercase; letter-spacing: .05em;
                             color: var(--muted); margin-bottom: 5px; }}
    .rd-tint-strong {{ background: rgba(42,143,95,.07); }}
    .rd-tint-mod {{ background: rgba(199,154,24,.08); }}
    .rd-tint-weak {{ background: rgba(180,69,58,.06); }}
    /* One-line "why" list */
    .eval-why-list {{ list-style: none; margin: 12px 0 4px; padding: 0;
                      display: flex; flex-direction: column; gap: 6px; }}
    .eval-why-list li {{ font-size: 13px; }}
    .eval-score-pill {{ display: inline-block; min-width: 26px; text-align: center;
                        font-weight: 800; font-size: 12px; padding: 1px 7px;
                        border-radius: 999px; background: #eef2f6;
                        border: 1px solid var(--line); }}
    .eval-note {{ margin: 8px 0 0; }}
    /* Score vs Actual Enrollment Validation */
    .reg-badge {{ display: inline-block; margin: 0 0 8px; padding: 3px 10px;
                  border-radius: 999px; font-size: 11px; font-weight: 700;
                  color: var(--mid); background: rgba(180,83,9,.10);
                  border: 1px solid rgba(180,83,9,.25); }}
    .reg-cap {{ margin: 0 0 10px; }}
    .reg-svg {{ width: 100%; max-width: 560px; height: auto;
                display: block; margin: 4px 0 10px; }}
    /* Per-course historical enrollment trend cards */
    .trend-grid {{ display: grid; gap: 12px; margin: 8px 0 4px;
                   grid-template-columns: repeat(3, 1fr); }}
    @media (max-width: 760px) {{ .trend-grid {{ grid-template-columns: 1fr; }} }}
    .trend-card {{ border: 1px solid var(--line); border-radius: 10px;
                   padding: 10px 12px; background: #fff; }}
    .trend-card h4 {{ margin: 0 0 4px; font-size: 13px; }}
    .trend-svg {{ width: 100%; height: auto; display: block; margin: 2px 0 6px; }}
    .trend-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 3px 10px;
                    font-size: 12px; margin: 2px 0 6px; }}
    .trend-stats div {{ display: flex; justify-content: space-between; gap: 6px; }}
    .trend-stats span {{ color: var(--muted); }}
    .trend-note {{ font-size: 11.5px; margin: 0; }}
    /* Enrollware data-quality grid */
    .dq-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
                margin: 6px 0 10px; }}
    @media (max-width: 760px) {{ .dq-grid {{ grid-template-columns: 1fr 1fr; }} }}
    .dq-item {{ display: flex; flex-direction: column; gap: 2px; padding: 8px 10px;
                border: 1px solid var(--line); border-radius: 8px; background: #fff; }}
    .dq-item span {{ color: var(--muted); font-size: 11px; }}
    .dq-item strong {{ font-size: 16px; font-variant-numeric: tabular-nums; }}
    .dq-item.dq-warn {{ border-color: var(--mid); background: #fffaf2; }}
    .dq-item.dq-warn strong {{ color: var(--mid); }}
    .reg-stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 4px 0 6px; }}
    .reg-stat {{ display: flex; flex-direction: column; gap: 2px; min-width: 78px;
                 padding: 7px 11px; border: 1px solid var(--line);
                 border-radius: 8px; background: #fff; }}
    .reg-stat-label {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                       letter-spacing: .04em; color: var(--muted); }}
    .reg-stat strong {{ font-size: 15px; }}
    .reg-warning {{ margin: 6px 0; }}
    .reg-note {{ margin: 10px 0 0; font-style: italic; font-size: 12px; }}
    /* ZIP demand visualization */
    .zip-dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%;
                margin-right: 6px; vertical-align: middle; }}
    .zip-ok {{ background: var(--good); }}
    .zip-missing {{ background: var(--low); }}
    .zip-report-table td:first-child {{ white-space: nowrap; }}
    .zip-map-svg {{ width: 100%; max-width: 560px; height: auto; display: block;
                    margin: 4px 0 10px; border-radius: 8px; }}
    .zip-missing-list {{ margin: 8px 0 0; }}
    .zip-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 0; }}
    .zip-chips .badge {{ font-weight: 600; }}
    /* Four score->outcome validation scatters in a 2-up grid */
    .zip-charts-grid {{ display: grid; grid-template-columns: repeat(2, 1fr);
                        gap: 14px; margin: 8px 0 4px; }}
    @media (max-width: 760px) {{ .zip-charts-grid {{ grid-template-columns: 1fr; }} }}
    .zip-chart-card {{ border: 1px solid var(--line); border-radius: 10px;
                       padding: 10px 12px; background: #fff; }}
    .zip-chart-card h4 {{ margin: 0 0 6px; font-size: 12.5px; }}
    .zip-chart-card .reg-svg {{ max-width: 100%; }}
    .zip-chart-card .reg-stats {{ margin-top: 2px; }}
    .zip-chart-wide {{ margin: 8px 0 4px; }}
    .zip-quadrant {{ margin: 8px 0 0; padding-left: 18px; font-size: 12px;
                     color: var(--ink); }}
    .zip-quadrant li {{ margin: 2px 0; }}
    /* Foldaway detail blocks */
    details.eval-details, details.course-details {{ margin-top: 12px; }}
    details.eval-details > summary, details.course-details > summary {{
        cursor: pointer; font-weight: 700; font-size: 13px; color: var(--accent);
        list-style: none; padding: 6px 0; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    details.eval-details > summary::before,
    details.course-details > summary::before {{ content: "▸ "; }}
    details[open].eval-details > summary::before,
    details[open].course-details > summary::before {{ content: "▾ "; }}
    .course-details-body .summary-section {{ background: #fafbfd; margin: 12px 0; }}
    @media (max-width: 640px) {{
      .eval-row {{ grid-template-columns: 110px 1fr 32px; }}
      .eval-rec {{ grid-template-columns: 1fr; }}
    }}
    /* Candidate-type badges */
    .ct-verified {{ background: var(--good); }}
    .ct-area {{ background: var(--accent); }}
    .ct-landmark {{ background: var(--mid); }}
    .ct-invalid {{ background: var(--low); }}
    /* Validation chips */
    .chips {{ margin: 6px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{ display: inline-block; padding: 2px 8px; border-radius: 6px;
             font-size: 11px; font-weight: 600; border: 1px solid var(--line); }}
    .chip-yes {{ background: #052e16; color: #4ade80; border-color: #14532d; }}
    .chip-no {{ background: #1f2937; color: #9ca3af; }}
    .chip-warn {{ background: #3f2d07; color: #fbbf24; border-color: #78510f; }}
    .exec-state {{ margin: 4px 0 0; font-weight: 700; color: var(--accent); }}
    .score-box {{ min-width: 104px; text-align: center; border: 1px solid var(--line);
                  padding: 10px; border-radius: 8px; }}
    .score-box strong {{ display: block; font-size: 26px; }}
    .score-box span {{ color: var(--muted); font-size: 12px; }}
    .score-box .site-line {{ margin-top: 6px; font-size: 12px; color: var(--muted);
                             border-top: 1px solid var(--line); padding-top: 6px; }}
    .not-validated {{ color: var(--mid); font-weight: 700; }}
    .quick-read p {{ margin: 2px 0; }}
    /* Score bars */
    .meters {{ margin: 6px 0; }}
    .meter {{ display: flex; align-items: center; gap: 8px; margin: 3px 0; }}
    .meter-label {{ width: 150px; font-size: 12px; }}
    .meter-track {{ flex: 1; height: 12px; background: var(--soft);
                    border-radius: 6px; overflow: hidden; }}
    .meter-fill {{ display: block; height: 100%; }}
    .bar-good {{ background: var(--good); }}
    .bar-mid {{ background: var(--mid); }}
    .bar-low {{ background: var(--low); }}
    .meter-val {{ width: 34px; text-align: right; font-variant-numeric: tabular-nums;
                  font-size: 12px; }}
    .interp-grid {{ display: grid; grid-template-columns: repeat(4, 1fr);
                    gap: 8px; margin: 6px 0; }}
    .interp-grid div {{ background: var(--soft); border-radius: 6px;
                        padding: 8px; }}
    .interp-grid span {{ display: block; color: var(--muted); font-size: 11px; }}
    .zip-bars {{ margin-top: 8px; }}
    .zip-bar {{ display: flex; align-items: center; gap: 8px; margin: 3px 0; }}
    .zip-bar-label {{ flex: 0 0 70px; font-size: 12px; }}
    .zip-bar-track {{ flex: 1; background: #eef1f4; border-radius: 4px; height: 12px; overflow: hidden; }}
    .zip-bar-fill {{ display: block; height: 100%; background: #2a8f5f; }}
    .zip-bar-value {{ flex: 0 0 auto; font-size: 12px; color: #555; }}
    .checklist {{ list-style: none; margin-left: 0; }}
    .checklist li::before {{ content: "\\2610  "; }}
    .links {{ display: flex; gap: 12px; flex-wrap: wrap; }}
    .photo-meta {{ color: var(--muted); overflow-wrap: anywhere; font-size: 12px; }}
    details {{ margin: 10px 0; }}
    summary {{ cursor: pointer; font-weight: 700; color: var(--accent); }}
    @media print {{
      body {{ background: #fff; }}
      main {{ padding: 0; max-width: none; }}
      .toc {{ display: none; }}
      .candidate-card, .summary-section {{ border-radius: 0; box-shadow: none; }}
      details {{ }}
      details > summary {{ display: none; }}
      details > *:not(summary) {{ display: block !important; }}
      a {{ color: inherit; text-decoration: underline; }}
    }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      .candidate-card header {{ display: block; }}
      .score-box {{ text-align: left; margin-top: 10px; }}
      .exec-grid {{ grid-template-columns: 1fr; }}
      .interp-grid {{ grid-template-columns: 1fr 1fr; }}
      .meter-label {{ width: 110px; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="report-header" id="top">
    <h1>{_esc(title)}</h1>
    <p class="muted">Generated {generated} · Mode: {_esc(mode)} · Style: {_esc(style)} ·
       Candidates shown: {len(candidates)}</p>
  </section>
  {_toc_nav(context, bool(candidate_cards))}
  <div class="accuracy">Every field is collected from a cited source or marked
    unknown. Revenue and student bands are estimated. All interpretation is
    derived deterministically from collected signals — no figures are
    invented. API keys and sensitive query tokens are stripped from links.</div>
  <span id="exec" class="anchor"></span>{_executive_panel(report_interp)}
  {_center_recommendation_layer(payload)}
  <span id="ai" class="anchor"></span>{_ai_summary_section(context)}
  {render_map_section(candidates, context)}
  <span id="areas" class="anchor"></span>{_city_rankings(context)}
  <span id="course" class="anchor"></span>{_course_performance_section(context)}
  <span id="zipdemand" class="anchor"></span>{_zip_demand_report_section(context)}
  {_data_quality_html(context.get("enrollware_data_quality"))}
  <span id="candidates" class="anchor"></span>
  <section class="summary-section flush">
    <h2>Candidate ranking</h2>
    {candidate_cards if candidate_cards else '<p class="muted">No candidates available.</p>'}
  </section>
  <span id="sources" class="anchor"></span>{_compact_source_audit(candidates, style, session_as_of=cache_session_as_of)}
</main>
</body>
</html>"""


def load_json_report(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_html_report(payload: Dict[str, Any], path: Path,
                      top_n: Optional[int] = None,
                      title: str = "ALLCPR Site Intelligence Report",
                      report_style: Optional[str] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_html_report(payload, top_n=top_n, title=title,
                           report_style=report_style),
        encoding="utf-8",
    )
    logger.info(f"Saved HTML report -> {path}")
