"""
Per-city Markdown site-selection report.

The report is a 15-second decision tool first and a data dump second. It opens
with an executive verdict and the next 3 actions, then gives each candidate a
plain-English quick read, score meters, an expansion-readiness call, ranked
demand signals, deterministic strategy labels and a decision checklist.

Three report styles (``--report-style``):
  - ``executive`` (default): concise, decision-ready, compact source audit
  - ``detailed``: the full rich tables, cleaner than before
  - ``debug``: detailed plus the raw per-candidate source audit + diagnostics

The underlying JSON/CSV always keep the full detailed data; only the Markdown
and HTML presentation changes between styles.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import SCORE_WEIGHTS
from app.reports.interpretation import (
    build_candidate_interpretation,
    build_report_interpretation,
    confidence_label,
    score_bar,
    score_meters,
)
from app.scoring.cohort_normalization import cohort_means_from_ranked
from app.utils.source_audit import (
    build_compact_source_audit,
    build_source_audit_rows,
)
from app.utils.logging_utils import get_logger
from app.utils.photo_url import build_photo_url
from app.utils.report_safety import strip_sensitive_query_params

logger = get_logger(__name__)

VALID_STYLES = ("executive", "detailed", "debug")


# ----- formatting helpers -------------------------------------------------- #

def _fmt_score(x) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else "unknown"


def _fmt_money(x) -> str:
    return f"${x:,.0f}" if isinstance(x, (int, float)) else "unknown"


def _fmt_rating(rating, reviews) -> str:
    r = f"★{rating}" if isinstance(rating, (int, float)) else "★ unknown"
    n = f"{reviews}" if isinstance(reviews, (int, float)) else "0"
    return f"{r} ({n})"


def _tier_badge(tier: str, label: str) -> str:
    return f"`{tier}` — {label}"


def _md_safe(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _maps_link_md(profile_dict: Dict, fallback_lat: Optional[float],
                  fallback_lon: Optional[float]) -> str:
    """Return a markdown link to Google Maps for the place."""
    url = profile_dict.get("google_maps_url") or ""
    if not url and profile_dict.get("place_id"):
        url = (f"https://www.google.com/maps/place/?q=place_id:"
               f"{profile_dict['place_id']}")
    if (not url and fallback_lat is not None and fallback_lon is not None):
        url = f"https://www.google.com/maps?q={fallback_lat},{fallback_lon}"
    url = strip_sensitive_query_params(url)
    return f"[Maps]({url})" if url else "_no link_"


def _photo_md(profile_dict: Dict) -> str:
    photos = profile_dict.get("photos") or []
    if not photos:
        return "Image: _unavailable_"
    ref = photos[0].get("photo_reference")
    # Saved reports must never embed Google photo URLs because those URLs carry
    # API keys. Keep metadata only; a private server-side proxy can resolve it.
    url = build_photo_url(ref, key_safe=True)
    if not url:
        return "Image: _photo metadata stored; URL hidden in key-safe mode_"
    return f"![Place photo]({url})"


# ----- anchor card --------------------------------------------------------- #

def _anchor_card(profile: Dict) -> List[str]:
    out: List[str] = []
    anchor: Optional[Dict] = profile.get("anchor")  # type: ignore[assignment]
    if not anchor:
        out.append("_No nearby anchor found; using coordinates only._")
        out.append("")
        return out
    out.append(f"**Recommended anchor:** {anchor.get('name') or 'unknown'}")
    out.append("")
    out.append(f"- **Full address:** {anchor.get('formatted_address') or 'unknown'}")
    out.append(
        f"- **Google Maps:** "
        f"{_maps_link_md(anchor, anchor.get('latitude'), anchor.get('longitude'))}"
    )
    website = strip_sensitive_query_params(anchor.get("website") or "")
    out.append(f"- **Website:** {website if website else '_unavailable_'}")
    phone = anchor.get("phone_number")
    out.append(f"- **Phone:** {phone if phone else '_unavailable_'}")
    out.append(
        f"- **Rating:** "
        f"{_fmt_rating(anchor.get('rating'), anchor.get('user_ratings_total'))}"
    )
    cat = anchor.get("category") or ""
    if cat:
        out.append(f"- **Anchor type:** {cat.replace('anchor:', '')}")
    out.append("")
    out.append(_photo_md(anchor))
    out.append("")
    return out


# ----- demand driver tables ------------------------------------------------ #

DEMAND_GROUPS: List[Tuple[str, str, List[str]]] = [
    ("Top hospitals & urgent care",
     "Healthcare facilities that drive BLS / first-responder demand.",
     ["hospital", "urgent_care", "medical_clinic"]),
    ("Top fire / EMS",
     "First-responder stations — strong BLS recurring demand.",
     ["fire_station", "ems"]),
    ("Top healthcare schools",
     "Pipeline of nursing / CNA / EMT / medical students.",
     ["nursing_school", "medical_school", "dental_school",
      "cna_training", "emt_training", "healthcare_training"]),
    ("Top colleges & universities",
     "General higher-ed demand for CPR/first-aid.",
     ["community_college", "university"]),
    ("Top institutional demand",
     "Childcare / senior care / dental clinics with mandated CPR.",
     ["childcare_center", "senior_care", "dental_clinic", "physical_therapy"]),
]


def _demand_group_table(profile: Dict, keys: List[str], max_rows: int = 8
                        ) -> List[str]:
    rows: List[Tuple[float, Dict, str]] = []
    top_places = profile.get("demand_top_places") or {}
    for k in keys:
        for p in (top_places.get(k) or [])[:max_rows]:
            d = p.get("distance_miles")
            d = d if isinstance(d, (int, float)) else 9999.0
            rows.append((d, p, k))
    rows.sort(key=lambda t: t[0])
    rows = rows[:max_rows]

    if not rows:
        return ["_No places found in this category._", ""]

    out = [
        "| Type | Name | Address | Distance | Rating | Phone | Website | Link |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, p, k in rows:
        dist = p.get("distance_miles")
        dist_str = f"{dist} mi" if isinstance(dist, (int, float)) else "unknown"
        out.append(
            f"| {k.replace('_', ' ')} "
            f"| {_md_safe(p.get('name') or 'unknown')} "
            f"| {_md_safe(p.get('formatted_address') or 'unknown')} "
            f"| {dist_str} "
            f"| {_fmt_rating(p.get('rating'), p.get('user_ratings_total'))} "
            f"| {_md_safe(p.get('phone_number') or 'unknown')} "
            f"| {_md_safe(strip_sensitive_query_params(p.get('website') or '') or 'unknown')} "
            f"| {_maps_link_md(p, p.get('latitude'), p.get('longitude'))} |"
        )
    out.append("")
    return out


# ----- competitor table ---------------------------------------------------- #

def _competitor_table(profile: Dict, max_rows: int = 10) -> List[str]:
    competitors = profile.get("competitors") or []
    if not competitors:
        return ["_No competitors found within radius._", ""]

    competitors = sorted(
        competitors,
        key=lambda c: c.get("distance_miles") if c.get("distance_miles") is not None
        else 9999.0,
    )[:max_rows]

    out = [
        "| Name | Address | Distance | Rating | Phone | Website | Web signals | Hours | Link |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in competitors:
        dist = c.get("distance_miles")
        dist_str = f"{dist} mi" if isinstance(dist, (int, float)) else "unknown"
        hours_list = c.get("opening_hours_weekday_text") or []
        hours_str = _md_safe("; ".join(hours_list[:2])) if hours_list else "unknown"
        web = c.get("website_analysis") or {}
        web_bits = []
        if web.get("checked"):
            detected = web.get("detected") or []
            missing = web.get("missing") or []
            if detected:
                web_bits.append("detected: " + ", ".join(detected[:4]))
            if missing:
                web_bits.append("missing: " + ", ".join(missing[:4]))
        elif web:
            web_bits.append("unknown")
        else:
            web_bits.append("not checked")
        out.append(
            f"| {_md_safe(c.get('name') or 'unknown')} "
            f"| {_md_safe(c.get('formatted_address') or 'unknown')} "
            f"| {dist_str} "
            f"| {_fmt_rating(c.get('rating'), c.get('user_ratings_total'))} "
            f"| {_md_safe(c.get('phone_number') or 'unknown')} "
            f"| {_md_safe(strip_sensitive_query_params(c.get('website') or '') or 'unknown')} "
            f"| {_md_safe('; '.join(web_bits))} "
            f"| {hours_str} "
            f"| {_maps_link_md(c, c.get('latitude'), c.get('longitude'))} |"
        )
    out.append("")
    return out


def _accessibility_lines(profile: Dict, scored: Dict) -> List[str]:
    accessibility = profile.get("accessibility") or {}
    signals = accessibility.get("signals") or {}
    out = [
        "### Accessibility profile (real/proxy signals)",
        "",
        f"- Accessibility score: "
        f"{_fmt_score(scored['sub_scores'].get('accessibility_score'))} / 100",
    ]
    if not signals:
        out.append("- Accessibility signals: unknown")
        out.append("")
        return out
    labels = {
        "freeway_major_road_proximity": "Freeway / major road proximity",
        "transit_station_proximity": "Transit station proximity",
        "airport_business_corridor_proximity": "Airport / business corridor proximity",
        "shopping_center_plaza_proximity": "Shopping center / plaza proximity",
        "parking_proxy": "Parking proxy",
        "walkability_proxy": "Walkability proxy",
    }
    for key, label in labels.items():
        sig = signals.get(key) or {}
        if not isinstance(sig, dict):
            continue
        status = sig.get("status") or "unknown"
        dist = sig.get("distance_miles")
        dist_text = f"{dist} mi" if isinstance(dist, (int, float)) else "unknown"
        name = sig.get("nearest_name") or sig.get("nearby_places_1mi") or "unknown"
        notes = sig.get("notes") or ""
        out.append(
            f"- {label}: {status}; nearest/count: {name}; "
            f"distance: {dist_text}; {notes}"
        )
    out.append("")
    return out


# ----- interpretation-driven blocks ---------------------------------------- #

def _score_meters_block(scored: Dict) -> List[str]:
    out = ["### Score meters", "", "```text"]
    for meter in score_meters(scored):
        label = meter["label"].ljust(20)
        value = meter["value"]
        bar = score_bar(value, 10)
        vtext = f"{value:.0f}" if isinstance(value, (int, float)) else "n/a"
        out.append(f"{label}{bar} {vtext}")
    out.append("```")
    out.append("")
    return out


def _historical_performance_block(profile: Dict, scored: Dict) -> List[str]:
    hist = profile.get("historical_performance") or {}
    if not hist:
        return [
            "### Historical ALLCPR performance",
            "",
            "_No Enrollware history was loaded for this run._",
            "",
        ]

    score = (scored.get("sub_scores") or {}).get("historical_performance_score")
    avg = hist.get("average_students_per_class")
    fill = hist.get("fill_rate_percent")
    recent = hist.get("recent_activity") or {}
    latest = recent.get("latest_class_date") or "unknown"
    recent_count = recent.get("classes_last_180_days")
    recent_text = (
        f"{recent_count} class(es) in the latest 180-day window"
        if isinstance(recent_count, int) else "unknown recent activity"
    )

    out = ["### Historical ALLCPR performance", ""]
    out.append(
        f"**Historical score:** {_fmt_score(score)} / 100 "
        f"({hist.get('confidence', 'unknown')} confidence)  "
    )
    out.append(
        f"**Matched scope:** {hist.get('area_label') or 'unknown'} "
        f"via `{hist.get('match_type') or 'none'}`"
    )
    out.append("")
    out.append(
        f"- Classes matched: {hist.get('total_classes', 0)}; "
        f"total students: {hist.get('total_students') or 'unknown'}; "
        f"avg students/class: {_fmt_score(avg)}; fill rate: {_fmt_score(fill)}%"
    )
    out.append(f"- Latest class date: {latest}; {recent_text}.")
    courses = hist.get("course_type_frequency") or []
    if courses:
        course_text = ", ".join(
            f"{c.get('label')}: {c.get('classes')}" for c in courses[:5]
        )
        out.append(f"- Course type frequency: {course_text}.")
    for reason in (hist.get("reasons") or [])[:3]:
        out.append(f"- {reason}")
    out.append("")
    return out


def _quick_read_block(interp: Dict) -> List[str]:
    qr = interp.get("quick_read") or {}
    return [
        "### Quick read",
        "",
        f"**What this location is:** {qr.get('what', 'unknown')}  ",
        f"**Why it scores high:** {qr.get('why_high', 'unknown')}  ",
        f"**Why it may fail:** {qr.get('why_fail', 'unknown')}  ",
        f"**Best use case:** {qr.get('best_use', 'unknown')}  ",
        f"**Decision:** {qr.get('decision', 'unknown')}",
        "",
    ]


def _readiness_block(interp: Dict) -> List[str]:
    er = interp.get("expansion_readiness") or {}
    out = [
        "### Expansion readiness",
        "",
        f"**`{er.get('readiness', 'Weak')}`**",
        "",
    ]
    for reason in er.get("reasons") or []:
        out.append(f"- {reason}")
    out.append("")
    return out


def _demand_signals_table(rows: List[Dict]) -> List[str]:
    out = [
        "| Signal | Count | Business importance | Why it matters |",
        "|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {_md_safe(r['signal'])} | {r.get('count_display', r['count'])} "
            f"| {r['importance']} | {_md_safe(r['why'])} |"
        )
    return out


def _demand_signals_block(interp: Dict) -> List[str]:
    ds = interp.get("demand_signals") or {}
    high_value = ds.get("high_value") or []
    secondary = ds.get("secondary") or []
    out = ["### Highest-value demand signals", ""]
    if high_value:
        out.extend(_demand_signals_table(high_value))
    else:
        out.append("_No high-value (hospital / training / clinical) demand "
                   "signals found within 5 mi._")
    out.append("")
    if secondary:
        out.append("<details><summary>Secondary demand signals "
                   f"({len(secondary)})</summary>")
        out.append("")
        out.extend(_demand_signals_table(secondary))
        out.append("")
        out.append("</details>")
        out.append("")
    return out


def _strategy_block(interp: Dict) -> List[str]:
    out = ["### Best strategies", ""]
    strategies = interp.get("strategies") or []
    if not strategies:
        out.append("- No deterministic strategy match; lead with partnerships.")
        out.append("")
        return out
    for i, strat in enumerate(strategies, start=1):
        out.append(f"{i}. **{strat['label']}** — {strat['why']}")
    out.append("")
    return out


_EVAL_MD_COLUMNS = [
    ("historical_performance", "Historical"),
    ("public_demand", "Public demand"),
    ("course_relative_performance", "Course vs avg"),
    ("competition_gap", "Competition"),
    ("schedule_strength", "Schedule"),
    ("forecast_expected_students", "Forecast"),
]


def _evaluation_graph_markdown(graph: Optional[Dict]) -> List[str]:
    """Render the Phase 5 Course Opportunity Graph as a Markdown section.

    Deterministic weighted-evidence table per course type, with the per-component
    contributions and the plain-English "why" reasons. Missing components render
    as ``—`` (left out, not assumed). Returns ``[]`` when there is no graph.
    """
    if not graph:
        return []
    courses = graph.get("course_opportunity_graph") or []
    if not courses:
        return []

    out: List[str] = ["## Course Opportunity Graph", ""]
    out.append(
        "_Honest, deterministic weighted evidence per course type. We cannot "
        "backtest unknown future students, so instead of predicting we show "
        "**why** each course is strong or weak. Missing signals are shown as "
        "`—` (left out, not assumed)._"
    )
    out.append("")

    header = "| Course | Score | Recommendation | " + " | ".join(
        lbl for _, lbl in _EVAL_MD_COLUMNS) + " | Penalty |"
    sep = "|" + "---|" * (3 + len(_EVAL_MD_COLUMNS) + 1)
    out.append(header)
    out.append(sep)
    for c in courses:
        nodes = {n["key"]: n for n in c.get("nodes") or []}
        cells = []
        for key, _ in _EVAL_MD_COLUMNS:
            node = nodes.get(key)
            if not node or node.get("missing"):
                cells.append("—")
            else:
                cells.append(f"+{node.get('contribution', 0):.0f}")
        penalty_pts = (c.get("penalty") or {}).get("penalty_points", 0) or 0
        out.append(
            f"| {c.get('label')} | {c.get('final_score')} | "
            f"{c.get('display_group')} | " + " | ".join(cells)
            + f" | −{penalty_pts:.0f} |"
        )
    out.append("")

    out.append("**Why this recommendation?**")
    out.append("")
    for c in courses:
        reasons = list(c.get("reasons") or [])
        for n in (c.get("nodes") or []):
            reasons.extend(n.get("reasons") or [])
        if reasons:
            out.append(f"- **{c.get('label')}** ({c.get('display_group')}): "
                       + "; ".join(reasons[:4]))
    out.append("")

    notes = graph.get("confidence_notes") or []
    if notes:
        out.append("**Confidence notes:**")
        out.append("")
        for n in notes:
            out.append(f"- {n}")
        out.append("")
    return out


def _regression_validation_markdown(rv: Optional[Dict]) -> List[str]:
    """Render the 'Score vs Actual Enrollment Validation' Markdown section.

    A score-vs-actual-enrollment table, the regression read-out (slope,
    intercept, R², Pearson, Spearman) when ``n >= 3``, and an explicit honesty
    note. Returns ``[]`` when there is no payload.
    """
    if not rv:
        return []

    out: List[str] = ["## Score vs Actual Enrollment Validation", ""]
    out.append("_**Validation only — not a future guarantee.** Does a higher "
               "opportunity score line up with higher *historical* enrollment? "
               "The line is a simple least-squares fit, drawn only with 3+ "
               "usable points._")
    out.append("")

    def _fmt(v, signed: bool = False) -> str:
        if v is None:
            return "—"
        return f"{float(v):+.3f}" if signed else f"{float(v):.3f}"

    if rv.get("enough_data"):
        out.append("| Metric | Value |")
        out.append("|---|---|")
        out.append(f"| Usable points (n) | {rv.get('n', 0)} |")
        out.append(f"| Slope (m) | {_fmt(rv.get('slope'), signed=True)} |")
        out.append(f"| Intercept (b) | {_fmt(rv.get('intercept'), signed=True)} |")
        out.append(f"| R² | {_fmt(rv.get('r_squared'))} |")
        out.append(f"| Pearson | {_fmt(rv.get('pearson'), signed=True)} |")
        out.append(f"| Spearman | {_fmt(rv.get('spearman'), signed=True)} |")
        out.append("")
    else:
        warn = rv.get("warning") or \
            "Not enough historical outcome data for reliable regression."
        out.append(f"> ⚠ {warn} No regression line drawn.")
        out.append("")

    points = rv.get("points") or []
    if points:
        out.append(f"| {rv.get('x_label', 'Opportunity score')} "
                   f"| {rv.get('y_label', 'Actual historical enrollment')} "
                   "| Course |")
        out.append("|---|---|---|")
        for p in points:
            out.append(f"| {p.get('score')} | {p.get('actual_enrollment')} "
                       f"| {_md_safe(str(p.get('label', '')))} |")
        out.append("")

    if rv.get("note"):
        out.append(f"_{rv['note']}_")
        out.append("")
    return out


def _opportunity_gaps_block(interp: Dict) -> List[str]:
    """Render the deterministic opportunity-gap engine output."""
    gaps_block = interp.get("opportunity_gaps") or {}
    gaps = gaps_block.get("gaps") or []
    confidence = gaps_block.get("data_confidence") or "low"
    positioning = gaps_block.get("positioning") or ""

    out = ["### Opportunity gaps (vs. local competitors)", ""]
    if not gaps:
        out.append("_No competitor analysis available — gaps unknown._")
        out.append("")
        return out

    out.append(f"**Positioning hint:** {positioning}  ")
    out.append(f"**Data confidence:** {confidence}")
    out.append("")
    if confidence == "low":
        out.append("_Few or no competitor websites were fetched; treat gap "
                   "strengths as preliminary._")
        out.append("")

    actionable = [g for g in gaps if g["strength"] in ("strong", "moderate")]
    if not actionable:
        out.append("_No strong or moderate gaps detected — competitors cover "
                   "the basics._")
        out.append("")
        return out

    out.append("| Gap | Strength | Evidence | How to win |")
    out.append("|---|---|---|---|")
    for g in actionable:
        out.append(
            f"| {_md_safe(g['label'])} "
            f"| {g['strength']} "
            f"| {_md_safe(g['evidence'])} "
            f"| {_md_safe(g['recommendation'])} |"
        )
    out.append("")
    return out


def _market_frustrations_block(profile: Dict) -> List[str]:
    """Render the 'why competitors fail' complaint-theme analysis."""
    summary = profile.get("competition_summary") or {}
    frustrations = summary.get("top_market_frustrations") or []
    scanned = summary.get("reviews_scanned")
    if not frustrations:
        return []
    out = ["### Why nearby competitors get bad reviews", ""]
    confidence = summary.get("review_data_confidence") or "low"
    out.append(
        f"_Based on {scanned} competitor review excerpt(s); "
        f"data confidence: {confidence}. Negative reviews only._"
    )
    out.append("")
    out.append("| Complaint theme | Mentions | How ALLCPR can win |")
    out.append("|---|---|---|")
    for f in frustrations[:6]:
        out.append(
            f"| {_md_safe(f.get('label') or f.get('theme'))} "
            f"| {f.get('count', 0)} "
            f"| {_md_safe(f.get('opportunity') or '')} |"
        )
    out.append("")
    return out


def _competitor_interpretation_block(interp: Dict) -> List[str]:
    ci = interp.get("competitor_interpretation") or {}
    avg = ci.get("avg_rating")
    avg_text = f"{avg:.2f}" if isinstance(avg, (int, float)) else "unknown"
    return [
        "### Competitive interpretation",
        "",
        f"- Competitor density: **{ci.get('density', 'unknown')}** "
        f"({ci.get('competitor_count_5mi', 0)} within 5 mi)",
        f"- Competitor quality: **{ci.get('quality', 'unknown')}** "
        f"(avg rating {avg_text})",
        f"- Market gap: **{ci.get('market_gap', 'unknown')}**",
        "",
        ci.get("win_path", "Competitive landscape unknown."),
        "",
    ]


def _factor_decomposition_block(interp: Dict) -> List[str]:
    """Per-candidate "drivers of Δ" decomposition — explainability for ranking."""
    decomposition = interp.get("factor_decomposition") or []
    text = interp.get("factor_decomposition_text") or ""
    if not decomposition:
        return []
    out = ["### Drivers of this candidate's site score vs the cohort", ""]
    out.append(f"_{text}_")
    out.append("")
    out.append(
        "| Sub-score | This candidate | Cohort mean | Δ | Weight | Contribution to site Δ |"
    )
    out.append("|---|---|---|---|---|---|")
    for r in decomposition:
        delta = r["delta"]
        contrib = r["contribution_to_site_delta"]
        sign_delta = "+" if delta >= 0 else ""
        sign_contrib = "+" if contrib >= 0 else ""
        out.append(
            f"| {r['sub_score']} "
            f"| {r['value']:.1f} "
            f"| {r['cohort_mean']:.1f} "
            f"| {sign_delta}{delta:.1f} "
            f"| {r['weight']:.0%} "
            f"| {sign_contrib}{contrib:.2f} |"
        )
    out.append("")
    return out


def _confidence_dimensions_block(scored: Dict) -> List[str]:
    """Render per-dimension confidence: demographic, accessibility, rent, etc."""
    breakdown = scored.get("confidence_breakdown") or {}
    dimensions = breakdown.get("dimensions") or {}
    if not dimensions:
        return []
    out = ["### Confidence by dimension", ""]
    label_for = {
        "demographic": "Demographic (Census)",
        "accessibility": "Accessibility signals",
        "rent": "Commercial rent",
        "competition": "Competitor analysis depth",
        "demand": "Demand category coverage",
        "data_freshness": "Data freshness",
        "saturation": "Demand-count saturation",
        "catchment_overlap": "Catchment distinctness",
        "differentiation": "Cohort score spread",
    }
    out.append("| Dimension | Score | Read |")
    out.append("|---|---|---|")
    for key in ("demographic", "accessibility", "rent",
                "competition", "demand", "data_freshness",
                "saturation", "catchment_overlap", "differentiation"):
        v = dimensions.get(key)
        if not isinstance(v, (int, float)):
            continue
        read = "high" if v >= 70 else ("moderate" if v >= 40 else "low")
        out.append(
            f"| {label_for.get(key, key)} | {v:.0f}/100 | {read} |"
        )
    out.append("")
    return out


def _warnings_block(interp: Dict) -> List[str]:
    out = ["### Warnings", ""]
    warnings = interp.get("warnings") or []
    if not warnings:
        out.append("- No warnings flagged from the collected signals.")
    else:
        for w in warnings:
            out.append(f"- {w}")
    out.append("")
    return out


def _checklist_block(interp: Dict) -> List[str]:
    out = ["### Decision checklist before leasing", ""]
    for item in interp.get("decision_checklist") or []:
        out.append(f"- [ ] {item}")
    out.append("")
    return out


def _profitability_block(scored: Dict) -> List[str]:
    prof = scored.get("profitability_estimate") or {}
    out = [
        "### Profitability estimate (model-based — not measured)",
        "",
        "| Scenario | Est. monthly students | Est. monthly revenue |",
        "|---|---|---|",
        f"| Low  | {prof.get('students_low', 'unknown')} "
        f"| {_fmt_money(prof.get('revenue_low'))} |",
        f"| Mid  | {prof.get('students_mid', 'unknown')} "
        f"| {_fmt_money(prof.get('revenue_mid'))} |",
        f"| High | {prof.get('students_high', 'unknown')} "
        f"| {_fmt_money(prof.get('revenue_high'))} |",
        "",
        "_Assumptions:_",
    ]
    for n in prof.get("notes") or []:
        out.append(f"- {n}")
    out.append("")
    return out


def _format_as_of_display(as_of_iso: str) -> str:
    """Convert a full ISO timestamp to 'YYYY-MM-DD HH:MM UTC' for the report."""
    if not as_of_iso:
        return "unknown"
    try:
        from datetime import datetime, timezone
        # Tolerate both 'Z' suffix and offset.
        ts = as_of_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return as_of_iso


def _compact_source_audit_block(
    ranked: List[Tuple[Dict, Dict]],
    cache_session_as_of: Optional[Dict[str, str]] = None,
) -> List[str]:
    """One compact source-audit summary table for the whole report."""
    flat: List[Dict] = []
    for profile, _ in ranked:
        flat.extend(profile.get("sources") or [])
    rows = build_compact_source_audit(flat, session_as_of=cache_session_as_of)
    out = ["## Source audit (compact)", ""]
    if not rows:
        out.append("_No sources recorded._")
        out.append("")
        return out
    out.append("| Source | Records / fields | Quality | As of | Notes |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {_md_safe(r['source'])} | {_md_safe(r['detail'])} "
            f"| {r['quality']} "
            f"| {_md_safe(_format_as_of_display(r.get('data_as_of', '')))} "
            f"| {_md_safe(r['notes'])} |"
        )
    out.append("")
    out.append("_Detailed per-field source provenance is preserved in the JSON "
               "report._")
    out.append("")
    return out


def _source_audit_appendix(ranked: List[Tuple[Dict, Dict]], top_n: int = 10
                           ) -> List[str]:
    """Full per-field source audit — only rendered in debug style."""
    out = ["## Source audit appendix (debug)", ""]
    if not ranked:
        out.append("_No sources recorded._")
        out.append("")
        return out
    out.extend([
        "| Candidate | Source name | Source API / URL | Retrieved at | "
        "Quality | Fields populated | Confidence |",
        "|---|---|---|---|---|---|---|",
    ])
    for profile, _ in ranked[:top_n]:
        candidate = (profile.get("candidate_name")
                     or profile.get("candidate_id") or "unknown")
        rows = build_source_audit_rows(profile.get("sources") or [])
        if not rows:
            out.append(f"| {_md_safe(candidate)} | unknown | unknown | unknown "
                       f"| unknown | unknown | unknown |")
            continue
        for row in rows:
            fields = ", ".join(row.get("fields_populated") or []) or "unknown"
            out.append(
                f"| {_md_safe(candidate)} "
                f"| {_md_safe(row['source_name'])} "
                f"| {_md_safe(strip_sensitive_query_params(row['source_api_or_url']))} "
                f"| {_md_safe(str(row['retrieved_at']))} "
                f"| {_md_safe(row['source_quality'])} "
                f"| {_md_safe(fields)} "
                f"| {_md_safe(row['confidence'])} |"
            )
    out.append("")
    return out


# ----- per-candidate section ---------------------------------------------- #

def _candidate_section(profile: Dict, scored: Dict, rank: int,
                       report_style: str = "executive",
                       cohort_means: Optional[Dict[str, float]] = None) -> List[str]:
    interp = build_candidate_interpretation(profile, scored, cohort_means=cohort_means)
    out: List[str] = []
    anchor = profile.get("anchor") or {}
    viability = profile.get("viability") or {}
    if viability.get("needs_validation"):
        title = profile.get("candidate_name") or "Needs commercial site validation"
    else:
        title = (anchor.get("name") if anchor else profile.get("candidate_name")) \
            or profile.get("candidate_name") or profile.get("candidate_id")
    out.append(f"## Rank #{rank} — {title}")
    out.append("")

    conf = scored["sub_scores"].get("confidence_score")
    readiness = (interp.get("expansion_readiness") or {}).get("readiness", "Weak")
    delta = profile.get("score_delta_vs_mean")
    delta_label = profile.get("score_delta_label") or ""
    if isinstance(delta, (int, float)) and delta_label:
        sign = "+" if delta > 0 else ""
        delta_text = f" · Δ vs cohort mean **{sign}{delta:.1f}** ({delta_label})"
    else:
        delta_text = ""
    _area = scored.get("area_score", scored.get("site_score"))
    _site = scored.get("site_score")
    _site_txt = (f"**{_fmt_score(_site)}/100** (validated)"
                 if isinstance(_site, (int, float)) else "_Not validated_")
    out.append(
        f"{_tier_badge(scored['tier'], scored['tier_label'])} · "
        f"`{scored.get('candidate_type_label') or scored.get('candidate_type') or ''}` · "
        f"{scored.get('executive_state') or ''} · Readiness: `{readiness}`"
    )
    out.append(
        f"Area score **{_fmt_score(_area)}/100** · Site score {_site_txt} · "
        f"Confidence {_fmt_score(conf)} ({confidence_label(conf)})"
        f"{delta_text}"
    )
    out.append("")

    # Decision-first blocks (every style).
    out.extend(_quick_read_block(interp))
    out.extend(_score_meters_block(scored))
    out.extend(_historical_performance_block(profile, scored))
    out.extend(_readiness_block(interp))
    out.extend(_demand_signals_block(interp))
    out.extend(_strategy_block(interp))
    out.extend(_competitor_interpretation_block(interp))
    out.extend(_market_frustrations_block(profile))
    out.extend(_opportunity_gaps_block(interp))

    # Why this location (rationale).
    out.append("### Why this location")
    out.append("")
    for line in (scored.get("rationale") or [])[:6]:
        out.append(f"- {line}")
    out.append("")

    out.extend(_profitability_block(scored))
    out.extend(_factor_decomposition_block(interp))
    out.extend(_confidence_dimensions_block(scored))
    out.extend(_warnings_block(interp))
    out.extend(_checklist_block(interp))

    if report_style == "executive":
        return out

    # ----- detailed / debug only ----- #
    out.append("### Anchor & coordinates")
    out.append("")
    out.extend(_anchor_card(profile))
    out.append(
        f"**Coordinates:** {profile['latitude']:.5f}, {profile['longitude']:.5f}  "
    )
    out.append(f"**Candidate ID:** `{profile['candidate_id']}`")
    out.append("")

    # Full score breakdown.
    out.append("### Score breakdown")
    out.append("")
    out.append("| Metric | Value | Weight |")
    out.append("|---|---|---|")
    for key, w in SCORE_WEIGHTS.items():
        out.append(f"| {key} | {_fmt_score(scored['sub_scores'].get(key))} "
                   f"| {w:.0%} |")
    out.append(f"| **area_score** | **{_fmt_score(scored.get('area_score', scored.get('site_score')))}** "
               f"| 100% |")
    _site_val = scored.get("site_score")
    out.append(
        f"| **site_score** | **{_fmt_score(_site_val) if isinstance(_site_val, (int, float)) else 'Not validated'}** "
        f"| gated |"
    )
    out.append(
        f"| job_certification_demand_score (separate) "
        f"| {_fmt_score(scored['sub_scores'].get('job_certification_demand_score'))} "
        f"| informational |"
    )
    out.append(f"| confidence_score (separate) | {_fmt_score(conf)} "
               f"| informational |")
    out.append("")

    # Demand-driver tables.
    out.append("### Key nearby demand drivers")
    out.append("")
    for group_title, blurb, keys in DEMAND_GROUPS:
        out.append(f"#### {group_title}")
        if blurb:
            out.append(f"_{blurb}_")
        out.append("")
        out.extend(_demand_group_table(profile, keys, max_rows=6))

    # Competitors.
    out.append("### Main CPR / BLS competitors")
    out.append("")
    out.extend(_competitor_table(profile, max_rows=10))

    # Job posting certification demand.
    out.append("### Job posting certification demand")
    out.append("")
    job = scored.get("job_demand") or {}
    job_block = profile.get("job_demand") or {}
    out.append(
        f"- job_certification_demand_score: "
        f"{_fmt_score(job.get('job_certification_demand_score'))}"
    )
    out.append(
        f"- job_demand_data_confidence: "
        f"{job.get('job_demand_data_confidence') or 'unknown'}"
    )
    out.append(
        f"- active_postings_count: "
        f"{job.get('active_postings_count') if job.get('active_postings_count') is not None else 'unknown'}"
    )
    out.append(
        f"- certification_postings_count: "
        f"{job.get('certification_postings_count') if job.get('certification_postings_count') is not None else 'unknown'}"
    )
    top_employers = job.get("top_employers") or []
    if top_employers:
        out.append("- top_employers: " + "; ".join(
            f"{e.get('employer')} ({e.get('posting_count')})"
            for e in top_employers[:5]
        ))
    else:
        out.append("- top_employers: unknown")
    sample_postings = job_block.get("sample_postings") or []
    if sample_postings:
        out.append("")
        out.append("| Employer | Title | Cert signals | Role signals | Source |")
        out.append("|---|---|---|---|---|")
        for posting in sample_postings[:5]:
            certs = ", ".join(posting.get("certification_signals") or []) or "none"
            roles = ", ".join(posting.get("role_signals") or []) or "none"
            source = strip_sensitive_query_params(posting.get("source_url") or "")
            link = f"[source]({source})" if source else "unknown"
            out.append(
                f"| {_md_safe(posting.get('employer') or 'unknown')} "
                f"| {_md_safe(posting.get('title') or 'unknown')} "
                f"| {_md_safe(certs)} "
                f"| {_md_safe(roles)} "
                f"| {link} |"
            )
    out.append("")

    out.extend(_accessibility_lines(profile, scored))

    # Economic profile.
    out.append("### Economic profile (Census ACS)")
    out.append("")
    census = (profile.get("economy") or {}).get("census") or {}
    cvals = census.get("values") or {}
    cind = census.get("indicators") or {}
    out.append(f"- Geography: {census.get('geo_desc') or 'unresolved'}")
    for k in ("population", "median_household_income", "median_age"):
        v = cvals.get(k)
        if v is None:
            out.append(f"- {k}: unknown")
        elif k == "median_household_income":
            out.append(f"- {k}: {_fmt_money(v)}")
        else:
            out.append(f"- {k}: {v:,.0f}")
    for k in ("healthcare_employment_share", "bachelors_or_higher_share",
              "working_age_share", "employment_rate"):
        v = cind.get(k)
        if isinstance(v, (int, float)):
            out.append(f"- {k}: {v:.1%}")
        else:
            out.append(f"- {k}: unknown")
    out.append("")

    # BLS labor market (county-level healthcare workforce).
    labor = (profile.get("economy") or {}).get("labor") or {}
    lvals = labor.get("values") or {}
    if any(v is not None for v in lvals.values()):
        out.append("### Healthcare workforce (BLS QCEW, county-level)")
        out.append("")
        emp = lvals.get("healthcare_employment_count")
        lq = lvals.get("healthcare_employment_lq")
        wage = lvals.get("avg_weekly_wage_healthcare")
        yr = lvals.get("data_year")
        out.append(f"- Healthcare employment: "
                   f"{emp:,.0f}" if isinstance(emp, (int, float)) else
                   "- Healthcare employment: unknown")
        if isinstance(lq, (int, float)):
            conc = ("above" if lq > 1.05 else
                    "below" if lq < 0.95 else "near")
            out.append(f"- Workforce concentration (location quotient): "
                       f"{lq:.2f} ({conc} national average)")
        if isinstance(wage, (int, float)):
            out.append(f"- Avg weekly healthcare wage: {_fmt_money(wage)}")
        if yr:
            out.append(f"- Source year: {yr}")
        out.append("")

    # Rent override.
    rent = scored.get("rent") or {}
    out.append("### Commercial rent override")
    out.append("")
    out.append(f"- rent_score: {_fmt_score(rent.get('rent_score'))}")
    out.append(f"- rent_data_confidence: {rent.get('rent_data_confidence') or 'unknown'}")
    out.append(f"- rent_source: "
               f"{strip_sensitive_query_params(rent.get('rent_source') or '') or 'unknown'}")
    out.append(f"- rent_notes: {rent.get('rent_notes') or 'unknown'}")
    out.append("")

    # Automated rent estimate (model proxy — NOT a cited quote).
    est = scored.get("rent_estimate") or {}
    if est:
        out.append("### Estimated rent pressure (model — not a cited quote)")
        out.append("")
        idx = est.get("rent_pressure_index")
        if isinstance(idx, (int, float)):
            band = ("high" if idx >= 66 else "moderate" if idx >= 33 else "low")
            out.append(f"- Rent-pressure index: {idx:.0f}/100 ({band} likely cost)")
        dollars = est.get("estimated_rent_per_sqft")
        if isinstance(dollars, (int, float)):
            out.append(f"- Estimated rent: ~${dollars:,.0f}/sqft/yr "
                       f"_(estimated from {est.get('anchor_count')} cited "
                       f"anchor(s); validate with a broker)_")
        else:
            out.append("- Estimated $/sqft: unavailable — add one cited rent "
                       "point to data/raw/rent_overrides.csv to calibrate the "
                       "index into dollars.")
        out.append("")

    # Tier reasoning.
    out.append("### Recommendation tier reasoning")
    out.append("")
    for r in scored.get("tier_reasons") or []:
        out.append(f"- {r}")
    out.append("")

    if report_style == "debug":
        out.append("### Raw diagnostics (debug)")
        out.append("")
        missing = profile.get("missing_fields") or []
        out.append(f"- missing_fields ({len(missing)}): "
                   f"{', '.join(missing) if missing else 'none'}")
        rows = build_source_audit_rows(profile.get("sources") or [])
        out.append(f"- raw source records: {len(rows)}")
        out.append("")

    sources = profile.get("source_urls") or []
    if sources:
        out.append("**Sources cited:**")
        out.append("")
        for url in sources:
            out.append(f"- {strip_sensitive_query_params(url)}")
        out.append("")

    return out


# ----- executive verdict + next actions ------------------------------------ #

def _ai_summary_block(ai_summary: Optional[Dict]) -> List[str]:
    """Render the optional AI-generated narrative (OpenAI/Groq), if present."""
    if not ai_summary or not ai_summary.get("text"):
        return []
    provider = ai_summary.get("provider") or "llm"
    model = ai_summary.get("model") or ""
    out = ["## AI executive summary", ""]
    out.append(
        f"_AI-generated narrative ({_md_safe(provider)}/{_md_safe(model)}) — "
        f"rephrases the deterministic analysis below; introduces no new figures._"
    )
    out.append("")
    for para in str(ai_summary["text"]).split("\n"):
        if para.strip():
            out.append(para.strip())
            out.append("")
    return out


def _executive_verdict_block(report_interp: Dict) -> List[str]:
    ev = report_interp.get("executive_verdict")
    out = ["## Executive verdict", ""]
    if not ev:
        out.append("_No candidates were evaluated._")
        out.append("")
        return out
    out.append(f"**Best candidate:** {ev['best_candidate']}  ")
    out.append(f"**Verdict:** {ev['verdict']}  ")
    out.append(f"**Expansion readiness:** {ev['expansion_readiness']}  ")
    out.append(f"**Why it matters:** {ev['why_it_matters']}  ")
    out.append(f"**Biggest risk:** {ev['biggest_risk']}  ")
    out.append(f"**Best strategy:** {ev['best_strategy']}  ")
    out.append(f"**Confidence level:** {ev['confidence']}  ")
    out.append(f"**Before leasing:** {ev['before_leasing']}")
    out.append("")
    return out


def _next_actions_block(report_interp: Dict) -> List[str]:
    out = ["## Recommended next 3 actions", ""]
    for i, action in enumerate(report_interp.get("next_actions") or [], start=1):
        out.append(f"{i}. {action}")
    out.append("")
    return out


def _decision_matrix_block(report_interp: Dict, top_n: int = 10) -> List[str]:
    """Side-by-side executive comparison: advantage / risk / path / fit / difficulty."""
    rows = (report_interp.get("decision_matrix") or [])[:top_n]
    out = ["## Decision matrix", ""]
    if not rows:
        out.append("_No candidates to compare._")
        out.append("")
        return out
    out.append(
        "| Candidate | Tier | Readiness | Strongest advantage "
        "| Biggest risk | Fastest path to profitability "
        "| Best strategic fit | Launch difficulty |"
    )
    out.append(
        "|---|---|---|---|---|---|---|---|"
    )
    for r in rows:
        out.append(
            f"| {_md_safe(r.get('candidate') or 'unknown')} "
            f"| `{r.get('tier') or '?'}` "
            f"| {_md_safe(r.get('readiness') or 'Weak')} "
            f"| {_md_safe(r.get('strongest_advantage') or 'unknown')} "
            f"| {_md_safe(r.get('biggest_risk') or 'unknown')} "
            f"| {_md_safe(r.get('fastest_path_to_profitability') or 'unknown')} "
            f"| {_md_safe(r.get('best_strategic_fit') or 'unknown')} "
            f"| {_md_safe(r.get('launch_difficulty') or 'unknown')} |"
        )
    out.append("")
    return out


# ----- top-of-report tables ------------------------------------------------ #

def _top_n_table(ranked: List[Tuple[Dict, Dict]], top_n: int,
                 report_style: str, area_column: bool = False) -> List[str]:
    out: List[str] = []
    if report_style == "executive":
        header = "| Rank | Tier | Readiness | Anchor | Site | Demand | " \
                 "Comp Gap | Opportunity | Conf |"
        sep = "|---|---|---|---|---|---|---|---|---|"
        if area_column:
            header = "| Rank | Area | Tier | Readiness | Anchor | Site | " \
                     "Demand | Comp Gap | Conf |"
            sep = "|---|---|---|---|---|---|---|---|---|"
        out.append(header)
        out.append(sep)
        for i, (p, s) in enumerate(ranked[:top_n], start=1):
            sub = s["sub_scores"]
            if (p.get("viability") or {}).get("needs_validation"):
                anchor_name = p.get("candidate_name") \
                    or "Needs commercial site validation"
            else:
                anchor_name = (p.get("anchor") or {}).get("name") \
                    or p.get("candidate_name") or p["candidate_id"]
            readiness = build_candidate_interpretation(
                p, s)["expansion_readiness"]["readiness"]
            area = p.get("comparison_area") or p.get("city") or "unknown"
            if area_column:
                out.append(
                    f"| {i} | {_md_safe(area)} | `{s['tier']}` | {readiness} "
                    f"| {_md_safe(anchor_name)} "
                    f"| {_fmt_score(s['site_score'])} "
                    f"| {_fmt_score(sub['demand_score'])} "
                    f"| {_fmt_score(sub['competition_gap_score'])} "
                    f"| {_fmt_score(sub['confidence_score'])} |"
                )
            else:
                out.append(
                    f"| {i} | `{s['tier']}` | {readiness} "
                    f"| {_md_safe(anchor_name)} "
                    f"| {_fmt_score(s['site_score'])} "
                    f"| {_fmt_score(sub['demand_score'])} "
                    f"| {_fmt_score(sub['competition_gap_score'])} "
                    f"| {_fmt_score(sub['allcpr_opportunity_score'])} "
                    f"| {_fmt_score(sub['confidence_score'])} |"
                )
        out.append("")
        return out

    # detailed / debug — full wide table
    out.append(
        "| Rank | Tier | Anchor | Site | Demand | Training | Comp Gap | "
        "Opportunity | Profitability | Econ | Access | Conf | Lat,Lon |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, (p, s) in enumerate(ranked[:top_n], start=1):
        sub = s["sub_scores"]
        if (p.get("viability") or {}).get("needs_validation"):
            anchor_name = p.get("candidate_name") \
                or "Needs commercial site validation"
        else:
            anchor_name = (p.get("anchor") or {}).get("name") \
                or p.get("candidate_name") or p["candidate_id"]
        out.append(
            f"| {i} | `{s['tier']}` | {_md_safe(anchor_name)} "
            f"| {_fmt_score(s['site_score'])} "
            f"| {_fmt_score(sub['demand_score'])} "
            f"| {_fmt_score(sub['healthcare_training_ecosystem_score'])} "
            f"| {_fmt_score(sub['competition_gap_score'])} "
            f"| {_fmt_score(sub['allcpr_opportunity_score'])} "
            f"| {_fmt_score(sub['profitability_score'])} "
            f"| {_fmt_score(sub['economy_score'])} "
            f"| {_fmt_score(sub['accessibility_score'])} "
            f"| {_fmt_score(sub['confidence_score'])} "
            f"| {p['latitude']:.5f},{p['longitude']:.5f} |"
        )
    out.append("")
    return out


def _dense_mode_block(ranked: List[Tuple[Dict, Dict]]) -> List[str]:
    """When density_probe rescaled the radius, surface it at the top of the
    report so the reader knows the configured radius was overridden."""
    for profile, _ in ranked:
        probe = profile.get("density_probe") or {}
        if probe.get("is_dense"):
            return [
                f"> **Dense-metro mode active.** "
                f"Detected {probe.get('competitor_count')} CPR/BLS "
                f"competitors within the configured "
                f"{probe.get('configured_radius_miles', 0):.1f}-mile radius. "
                f"Auto-reduced to a neighborhood-scale catchment: "
                f"radius **{probe.get('effective_radius_miles', 0):.1f} mi**, "
                f"grid spacing "
                f"**{probe.get('effective_grid_spacing_miles', 0):.1f} mi**. "
                f"Re-run with --no-dense-mode to override.",
                "",
            ]
    return []


def _radius_warning_block(
    radius_miles: float,
    ranked: List[Tuple[Dict, Dict]],
) -> List[str]:
    """Top-of-report warning when radius is large for a dense urban area.

    A 7-mile search around downtown San Francisco blends Mission, SoMa,
    Western Addition, Marina, and Pacific Heights into one bucket — the
    demand counts inflate, every candidate looks similar, and the ranking
    is no longer meaningful at the neighborhood level. We surface this
    explicitly so the reader can shrink the radius.

    Skipped when dense_mode_block already explained the rescale.
    """
    if radius_miles < 5 or not ranked:
        return []
    # If dense-mode auto-rescaled, the dense_mode_block already explained it.
    for profile, _ in ranked:
        if (profile.get("density_probe") or {}).get("is_dense"):
            return []

    dense_count = 0
    for profile, _ in ranked:
        summary = profile.get("competition_summary") or {}
        buckets = summary.get("competitor_count_by_bucket_mi") or {}
        comp_5mi = buckets.get(5) or buckets.get("5") or 0
        try:
            if int(comp_5mi) >= 10:
                dense_count += 1
        except (TypeError, ValueError):
            continue

    if dense_count == 0:
        return []

    out = [
        "> **Large radius warning.** "
        f"The configured radius ({radius_miles:.1f} mi) is wide for a "
        f"dense urban metro — {dense_count} of {len(ranked)} candidate(s) "
        f"have ≥10 CPR/BLS competitors within 5 mi. Demand counts may blend "
        f"multiple neighborhoods together and inflate ranking similarity. "
        f"Consider re-running with --radius-miles 2-3 and tighter grid "
        f"spacing to surface meaningful neighborhood-level differentiation.",
        "",
    ]
    return out


def _accuracy_note() -> List[str]:
    return [
        "> **Accuracy note.** Every field is either collected from a cited "
        "source or marked _unknown_. Profitability bands are explicitly "
        "labeled _estimated_ and rely on configurable model assumptions. "
        "All interpretation below is derived deterministically from collected "
        "signals — no figures are invented. Validate with on-the-ground due "
        "diligence before any lease decision.",
        "",
    ]


# ----- public render functions --------------------------------------------- #

def render_markdown_report(
    city: str,
    state: str,
    radius_miles: float,
    ranked: List[Tuple[Dict, Dict]],
    top_n: int = 10,
    report_style: str = "executive",
    cache_session_as_of: Optional[Dict[str, str]] = None,
    ai_summary: Optional[Dict] = None,
    course_performance: Optional[Dict] = None,
) -> str:
    if report_style not in VALID_STYLES:
        report_style = "executive"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_interp = build_report_interpretation(ranked)
    cohort_means = report_interp.get("cohort_means") or cohort_means_from_ranked(ranked)
    out: List[str] = []

    out.append(f"# ALLCPR Site Intelligence Report — {city}, {state}")
    out.append("")
    out.append(f"_Generated {now}. Style: {report_style}. "
               f"Radius: {radius_miles} mi per candidate. "
               f"Candidates evaluated: {len(ranked)}._")
    out.append("")
    out.extend(_accuracy_note())
    out.extend(_dense_mode_block(ranked))
    out.extend(_radius_warning_block(radius_miles, ranked))

    # 15-second decision layer.
    out.extend(_executive_verdict_block(report_interp))
    out.extend(_ai_summary_block(ai_summary))
    out.extend(_next_actions_block(report_interp))
    out.extend(_decision_matrix_block(report_interp, top_n=top_n))

    # Top-N table.
    out.append(f"## Top {top_n} recommended areas")
    out.append("")
    out.extend(_top_n_table(ranked, top_n, report_style))

    if report_style in ("detailed", "debug"):
        out.append("### Map-ready coordinates")
        out.append("")
        out.append("```")
        for i, (p, _) in enumerate(ranked[:top_n], start=1):
            anchor_name = (p.get("anchor") or {}).get("name") \
                or p.get("candidate_name") or p["candidate_id"]
            out.append(f"{i}\t{p['latitude']:.6f}\t{p['longitude']:.6f}\t"
                       f"{anchor_name}")
        out.append("```")
        out.append("")

    # Per-candidate detail.
    out.append("## Per-candidate detail")
    out.append("")
    for i, (p, s) in enumerate(ranked[:top_n], start=1):
        out.extend(_candidate_section(p, s, i, report_style,
                                      cohort_means=cohort_means))
        out.append("---")
        out.append("")

    # Phase 5 — Course Opportunity Graph (when Enrollware course data exists).
    if course_performance and course_performance.get("evaluation_graph"):
        out.extend(_evaluation_graph_markdown(
            course_performance["evaluation_graph"]))

    # Score vs Actual Enrollment Validation — directly under the graph above.
    if course_performance and course_performance.get("regression_validation"):
        out.extend(_regression_validation_markdown(
            course_performance["regression_validation"]))

    # Source audit — compact always; full appendix only in debug.
    out.extend(_compact_source_audit_block(
        ranked[:top_n], cache_session_as_of=cache_session_as_of))
    if report_style == "debug":
        out.extend(_source_audit_appendix(ranked, top_n=top_n))

    # Final recommendation.
    out.append("## Final recommendation")
    out.append("")
    ev = report_interp.get("executive_verdict")
    if ev:
        top_profile = ranked[0][0]
        anchor_addr = (top_profile.get("anchor") or {}).get("formatted_address") \
            or "address unknown"
        out.append(
            f"Pursue **{ev['best_candidate']}** ({anchor_addr}) as the lead "
            f"candidate — verdict: {ev['verdict']}; expansion readiness: "
            f"{ev['expansion_readiness']}."
        )
        out.append("")
        out.append(f"{ev['best_strategy']} {ev['before_leasing']}")
    else:
        out.append("No data — increase radius or supply richer city input.")
    out.append("")

    return "\n".join(out)


def render_metro_comparison_report(
    state: str,
    radius_miles: float,
    ranked: List[Tuple[Dict, Dict]],
    city_rankings: List[Dict],
    top_n: int = 20,
    report_style: str = "executive",
    cache_session_as_of: Optional[Dict[str, str]] = None,
    ai_summary: Optional[Dict] = None,
) -> str:
    """Render one metro-level report with city and candidate rankings."""
    if report_style not in VALID_STYLES:
        report_style = "executive"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_interp = build_report_interpretation(ranked)
    cohort_means = report_interp.get("cohort_means") or cohort_means_from_ranked(ranked)
    out: List[str] = []
    out.append(f"# ALLCPR Metro Comparison Report — {state or 'multi-state'}")
    out.append("")
    out.append(
        f"_Generated {now}. Mode: metro_comparison. Style: {report_style}. "
        f"Radius: {radius_miles} mi per comparison area. "
        f"Deduplicated candidates evaluated: {len(ranked)}._"
    )
    out.append("")
    out.extend(_accuracy_note())
    out.extend(_dense_mode_block(ranked))
    out.extend(_radius_warning_block(radius_miles, ranked))

    out.extend(_executive_verdict_block(report_interp))
    out.extend(_ai_summary_block(ai_summary))
    out.extend(_next_actions_block(report_interp))
    out.extend(_decision_matrix_block(report_interp, top_n=top_n))

    out.append("## City / area ranking")
    out.append("")
    if city_rankings:
        out.append("| Rank | Area | Best candidate | Best site score | "
                   "Avg top score | Candidate count |")
        out.append("|---|---|---|---|---|---|")
        for row in city_rankings:
            out.append(
                f"| {row.get('city_rank')} "
                f"| {_md_safe(row.get('area') or 'unknown')} "
                f"| {_md_safe(row.get('best_candidate') or 'unknown')} "
                f"| {_fmt_score(row.get('best_site_score'))} "
                f"| {_fmt_score(row.get('avg_top_site_score'))} "
                f"| {row.get('candidate_count', 0)} |"
            )
    else:
        out.append("_No areas evaluated._")
    out.append("")

    out.append(f"## Top {top_n} candidates")
    out.append("")
    out.extend(_top_n_table(ranked, top_n, report_style, area_column=True))

    out.append("## Per-candidate detail")
    out.append("")
    for i, (p, s) in enumerate(ranked[:top_n], start=1):
        out.extend(_candidate_section(p, s, i, report_style,
                                      cohort_means=cohort_means))
        out.append("---")
        out.append("")

    out.extend(_compact_source_audit_block(
        ranked[:top_n], cache_session_as_of=cache_session_as_of))
    if report_style == "debug":
        out.extend(_source_audit_appendix(ranked, top_n=top_n))
    return "\n".join(out)


def write_markdown_report(text: str, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info(f"Saved Markdown report -> {path}")
