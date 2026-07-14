"""
Easy executive ("boss-facing") HTML report.

A second, simpler presentation layer over the *same* computed payload the full
technical report renders from. It introduces no new figures, no new scoring and
no new recommendation logic — it only re-arranges already-computed results into
a clean, Apple-style, decision-first page:

  - a one-line quick verdict + a few executive cards on the first screen,
  - the top candidates only (not every raw detail),
  - course intelligence (held-class benchmark table + trend charts),
  - a plain-English score-validation read-out,
  - everything heavy (methodology, sources, data-quality audit, full validation
    table, AI narrative) tucked behind <details>.

It deliberately reuses the full report's section renderers so the two reports
can never disagree on a number.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.evaluation.center_recommendations import (
    build_center_recommendations_from_report,
)
from app.reports.interpretation import build_report_interpretation
from app.reports.html_report import (
    _ai_summary_section,
    _compact_source_audit,
    _course_benchmark_html,
    _course_trends_html,
    _data_quality_html,
    _esc,
    _regression_scatter_svg,
    _resolve_style,
    _score,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Always-visible honest guardrails (kept short for the boss view).
_WARNINGS = (
    "Site not validated.",
    "Opportunity ranking, not a guaranteed prediction.",
    "High data confidence does not mean lease-ready.",
    "Future enrollment depends on ads, pricing, schedule timing, instructor "
    "availability, and student behavior.",
)

_HELD_NOTE = (
    "Enrollware historical performance uses completed held classes only. Future "
    "scheduled classes and zero-enrollment placeholder rows are excluded from "
    "enrollment averages."
)

_DECISION_CLASS = {
    "Open / Prioritize": "ok",
    "Test first": "warn",
    "Keep watching": "warn",
    "Avoid for now": "bad",
}


# --------------------------------------------------------------------------- #
# Small data helpers (read-only over the payload)
# --------------------------------------------------------------------------- #

def _area_label(context: Dict[str, Any], course: Dict[str, Any],
                ev: Dict[str, Any]) -> str:
    cities = context.get("cities")
    if isinstance(cities, (list, tuple)) and cities:
        return ", ".join(str(c) for c in cities if c)
    if isinstance(cities, str) and cities.strip():
        return cities.strip()
    if course.get("area_label"):
        return str(course["area_label"])
    # Fall back to the trailing city in the best-candidate title.
    best = str(ev.get("best_candidate") or "")
    return best.split(",")[-1].strip() if "," in best else best


def _top_recommendations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    co = payload.get("center_opening_recommendations")
    if not co:
        co = build_center_recommendations_from_report(payload, limit=5)
    return (co or {}).get("recommendations") or []


def _trend_takeaway(trends: Dict[str, Any]) -> str:
    rows = (trends or {}).get("trends") or []
    if not rows:
        return ""
    dir_by = {}
    for t in rows:
        dir_by.setdefault(t.get("trend_direction") or "insufficient data", []).append(
            t.get("course_type")
        )
    if len(dir_by) == 1:
        only = next(iter(dir_by))
        return (f"Across completed held months, ARC CPR, ARC BLS and AHA BLS are "
                f"all {only}.")
    parts = [f"{', '.join(c for c in courses if c)} {d}"
             for d, courses in dir_by.items()]
    return "Over completed held months: " + "; ".join(parts) + "."


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def _cross_links(full_name: str, easy_name: str) -> str:
    return (
        "<nav class=\"xlinks\">"
        f"<a href=\"{_esc(full_name)}\">Open full technical report →</a>"
        f"<a href=\"{_esc(easy_name)}\" class=\"current\">Easy executive report</a>"
        "</nav>"
    )


def _hero(area: str, ev: Dict[str, Any], top: Optional[Dict[str, Any]]) -> str:
    decision = (top or {}).get("decision_label") or ev.get("executive_state") or ""
    location = (top or {}).get("location_name") or ev.get("best_candidate") or ""
    course = (top or {}).get("course_type") or ""
    nxt = (top or {}).get("suggested_next_action") or ev.get("before_leasing") or ""
    # Avoid "near Near Dai Thanh…" when the location label already says "Near".
    loc_phrase = str(location)
    if not loc_phrase.lower().startswith("near"):
        loc_phrase = f"near {loc_phrase}"
    headline_bits = []
    if area and decision and location:
        headline_bits.append(f"{area}: {decision} {loc_phrase}.")
    elif decision and location:
        headline_bits.append(f"{decision} {loc_phrase}.")
    if course:
        headline_bits.append(f"Best launch course: {course}.")
    if nxt:
        headline_bits.append(nxt)
    headline = " ".join(headline_bits)
    return (
        "<section class=\"hero card\">"
        "<p class=\"eyebrow\">Quick verdict</p>"
        f"<p class=\"hero-line\">{_esc(headline)}</p>"
        "</section>"
    )


def _decision_cards(ev: Dict[str, Any], top: Optional[Dict[str, Any]]) -> str:
    top = top or {}
    cards = [
        ("Best candidate", top.get("location_name") or ev.get("best_candidate")),
        ("Decision", top.get("decision_label") or ev.get("executive_state"),
         _DECISION_CLASS.get(top.get("decision_label"), "")),
        ("Best course", top.get("course_type")),
        ("Area score", top.get("area_score")),
        ("Readiness", top.get("expansion_readiness") or ev.get("expansion_readiness")),
        ("Confidence", top.get("data_confidence_label") or ev.get("confidence")),
        ("Biggest risk", ev.get("biggest_risk")),
        ("Next action", top.get("suggested_next_action") or ev.get("before_leasing")),
    ]
    out = []
    for entry in cards:
        label, value = entry[0], entry[1]
        tone = entry[2] if len(entry) > 2 else ""
        if value in (None, ""):
            continue
        out.append(
            f"<div class=\"stat card tone-{tone}\"><span>{_esc(label)}</span>"
            f"<strong>{_esc(value)}</strong></div>"
        )
    return f"<section class=\"stat-grid\">{''.join(out)}</section>"


def _top_candidates(recs: List[Dict[str, Any]]) -> str:
    if not recs:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td><strong>{_esc(r.get('location_name'))}</strong></td>"
        f"<td><span class=\"pill {_DECISION_CLASS.get(r.get('decision_label'), '')}\">"
        f"{_esc(r.get('decision_label'))}</span></td>"
        f"<td>{_esc(r.get('course_type'))}</td>"
        f"<td>{_esc(r.get('area_score'))}</td>"
        f"<td>{_esc(r.get('expansion_readiness'))}</td>"
        f"<td>{_esc(r.get('suggested_next_action'))}</td>"
        "</tr>"
        for i, r in enumerate(recs[:5], start=1)
    )
    return (
        "<section class=\"card\">"
        "<h2>Center Opening Recommendation</h2>"
        "<p class=\"muted\">Top candidates only. Full per-candidate evidence is "
        "in the technical report.</p>"
        "<div class=\"scroll-x\"><table><thead><tr><th>#</th><th>Location</th>"
        "<th>Decision</th><th>Best course</th><th>Area score</th><th>Readiness</th>"
        f"<th>Next action</th></tr></thead><tbody>{rows}</tbody></table></div>"
        "</section>"
    )


def _course_intelligence(course: Dict[str, Any]) -> str:
    bench = _course_benchmark_html(course.get("course_enrollment_benchmarks"))
    trends_payload = course.get("course_enrollment_trends")
    trends = _course_trends_html(trends_payload)
    if not bench and not trends:
        return ""
    takeaway = _trend_takeaway(trends_payload or {})
    takeaway_html = f"<p class=\"takeaway\">{_esc(takeaway)}</p>" if takeaway else ""
    return (
        "<section class=\"card\">"
        "<h2>Course intelligence</h2>"
        f"<p class=\"muted\">{_esc(_HELD_NOTE)}</p>"
        + takeaway_html + bench + trends
        + "</section>"
    )


def _validation(course: Dict[str, Any]) -> str:
    rv = course.get("regression_validation")
    if not rv:
        return ""
    svg = _regression_scatter_svg(rv)
    n = rv.get("n")
    stats = [
        ("R²", rv.get("r_squared")),
        ("Pearson", rv.get("pearson")),
        ("Spearman", rv.get("spearman")),
        ("Sample size", n),
    ]
    chips = "".join(
        f"<div class=\"stat card\"><span>{_esc(k)}</span>"
        f"<strong>{_score(v) if k != 'Sample size' else _esc(v)}</strong></div>"
        for k, v in stats if v is not None
    )
    return (
        "<section class=\"card\">"
        "<h2>Score validation</h2>"
        "<p class=\"muted\">This checks whether higher opportunity scores have "
        "historically lined up with higher enrollment. It is a sanity check on "
        "the scoring, not a future guarantee.</p>"
        + (svg or "")
        + (f"<div class=\"stat-grid\">{chips}</div>" if chips else "")
        + "</section>"
    )


def _warnings_html() -> str:
    items = "".join(f"<li>{_esc(w)}</li>" for w in _WARNINGS)
    return (
        "<section class=\"card warn-card\">"
        "<h2>Before you act</h2>"
        f"<ul class=\"warns\">{items}</ul>"
        "</section>"
    )


def _details_block(title: str, inner: str) -> str:
    if not inner:
        return ""
    return (
        f"<details class=\"card\"><summary>{_esc(title)}</summary>"
        f"<div class=\"details-body\">{inner}</div></details>"
    )


def _collapsed_details(payload: Dict[str, Any], context: Dict[str, Any],
                       candidates: List[Dict[str, Any]], style: str,
                       full_name: str) -> str:
    methodology = (
        "<p>Scores are deterministic and computed once from collected signals; "
        "this easy report only re-presents them. Enrollment metrics use the "
        "held-class basis (completed months, real attendance). Revenue and "
        "student bands are estimates. No figures are invented.</p>"
        f"<p><a href=\"{_esc(full_name)}\">Open the full technical report</a> for "
        "per-candidate evidence, maps, source links and the full validation "
        "point table.</p>"
    )
    blocks = [
        _details_block("Methodology", methodology),
        _details_block("AI narrative", _ai_summary_section(context)),
        _details_block(
            "Data-quality audit",
            _data_quality_html(context.get("enrollware_data_quality")),
        ),
        _details_block(
            "Source evidence & raw links",
            _compact_source_audit(candidates, style,
                                  session_as_of=context.get("cache_session_as_of")),
        ),
    ]
    return "".join(b for b in blocks if b)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

def easy_output_path(main_output: Path) -> Path:
    """Derive the easy-report path from the main report path.

    ``sj_report.html`` -> ``sj_easy_report.html``;
    ``all_cities_report.html`` -> ``all_cities_easy_report.html``.
    Falls back gracefully for names that don't end in ``_report.html``.
    """
    main_output = Path(main_output)
    name = main_output.name
    if name.endswith("_report.html"):
        easy = name[: -len("_report.html")] + "_easy_report.html"
    elif name.endswith(".html"):
        easy = name[: -len(".html")] + "_easy.html"
    else:
        easy = name + "_easy"
    return main_output.with_name(easy)


def render_easy_html_report(
    payload: Dict[str, Any],
    title: str = "ALLCPR — Easy Executive Report",
    full_report_name: str = "report.html",
    easy_report_name: str = "easy_report.html",
    report_style: Optional[str] = None,
) -> str:
    """Render the boss-facing easy report HTML from the same report payload."""
    context = payload.get("context") or {}
    style = _resolve_style(context, report_style)
    candidates = list(payload.get("candidates") or [])
    course = context.get("course_performance") or {}

    report_interp = payload.get("report_interpretation")
    if not report_interp:
        ranked = [(c.get("profile") or {}, c.get("scored") or {})
                  for c in candidates]
        report_interp = build_report_interpretation(ranked)
    ev = report_interp.get("executive_verdict") or {}

    recs = _top_recommendations(payload)
    top = recs[0] if recs else None
    area = _area_label(context, course, ev)

    xlinks = _cross_links(full_report_name, easy_report_name)
    body = (
        xlinks
        + _hero(area, ev, top)
        + _decision_cards(ev, top)
        + _warnings_html()
        + _top_candidates(recs)
        + _course_intelligence(course)
        + _validation(course)
        + _collapsed_details(payload, context, candidates, style, full_report_name)
        + xlinks
    )

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<main>\n<header class=\"page-head\">\n<h1>{_esc(title)}</h1>\n"
        f"<p class=\"muted\">{_esc(area)}</p>\n</header>\n"
        f"{body}\n</main>\n</body>\n</html>\n"
    )


def write_easy_html_report(
    payload: Dict[str, Any], path: Path,
    title: str = "ALLCPR — Easy Executive Report",
    full_report_name: str = "report.html",
    report_style: Optional[str] = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_easy_html_report(
            payload, title=title, full_report_name=full_report_name,
            easy_report_name=path.name, report_style=report_style,
        ),
        encoding="utf-8",
    )
    logger.info(f"Saved easy executive report -> {path}")


# --------------------------------------------------------------------------- #
# Apple-style stylesheet (kept out of the f-string to avoid brace escaping)
# --------------------------------------------------------------------------- #

_CSS = """
:root{
  --ink:#1d1d1f; --muted:#6e6e73; --line:#e8e8ed; --bg:#f5f5f7; --card:#ffffff;
  --accent:#0071e3; --good:#1d8a3b; --mid:#b25000; --low:#c01927;
  --shadow:0 1px 3px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.05);
  --radius:18px;
}
*{box-sizing:border-box;}
html{scroll-behavior:smooth;}
body{margin:0;color:var(--ink);background:var(--bg);
  font:17px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;}
main{max-width:820px;margin:0 auto;padding:28px 20px 80px;}
.page-head{padding:18px 4px 6px;}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 4px;font-weight:700;}
h2{font-size:20px;letter-spacing:-.01em;margin:0 0 12px;font-weight:650;}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  margin:18px 0 8px;}
p{margin:0 0 10px;}
.muted{color:var(--muted);font-size:14px;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
.card{background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);
  padding:24px 26px;margin:18px 0;}
.eyebrow{font-size:13px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--accent);font-weight:650;margin:0 0 8px;}
.hero{padding:30px 30px;}
.hero-line{font-size:23px;line-height:1.4;letter-spacing:-.01em;margin:0;font-weight:550;}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0;}
.stat{padding:16px 18px;margin:0;display:flex;flex-direction:column;gap:6px;}
.stat span{font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}
.stat strong{font-size:19px;font-weight:600;letter-spacing:-.01em;line-height:1.25;}
.tone-ok strong{color:var(--good);} .tone-warn strong{color:var(--mid);}
.tone-bad strong{color:var(--low);}
.pill{display:inline-block;padding:3px 11px;border-radius:999px;font-size:13px;
  font-weight:600;background:var(--line);color:var(--ink);}
.pill.ok{background:#e3f3e8;color:var(--good);} .pill.warn{background:#fbeede;color:var(--mid);}
.pill.bad{background:#fbe4e6;color:var(--low);}
table{width:100%;border-collapse:collapse;font-size:14.5px;margin:6px 0 0;}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top;}
th{font-weight:600;color:var(--muted);font-size:13px;}
tbody tr:last-child td{border-bottom:0;}
td:not(:nth-child(2)):not(:last-child),th{font-variant-numeric:tabular-nums;}
.scroll-x{overflow-x:auto;}
.takeaway{font-size:16px;font-weight:550;margin:4px 0 14px;}
.warn-card{background:#fffdf6;border:1px solid #f1e6c8;}
.warns{margin:6px 0 0;padding-left:20px;} .warns li{margin:5px 0;color:var(--ink);font-size:15px;}
.xlinks{display:flex;gap:16px;flex-wrap:wrap;margin:4px 0 10px;font-size:15px;font-weight:550;}
.xlinks .current{color:var(--muted);pointer-events:none;}
details.card>summary{cursor:pointer;font-weight:600;list-style:none;}
details.card>summary::-webkit-details-marker{display:none;}
details.card>summary::before{content:"›";display:inline-block;margin-right:10px;
  transition:transform .15s;color:var(--accent);font-weight:700;}
details.card[open]>summary::before{transform:rotate(90deg);}
.details-body{margin-top:14px;}
/* Reused fragment classes from the full report, restyled clean */
.summary-section{margin:14px 0 0;} .summary-section h2{font-size:17px;}
.summary-section h3{margin-top:14px;}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
  font-weight:600;background:var(--line);color:var(--ink);}
.rd-strong,.tier-a{background:#e3f3e8;color:var(--good);}
.rd-moderate,.tier-c{background:#fbeede;color:var(--mid);}
.rd-weak,.tier-f{background:#fbe4e6;color:var(--low);}
.reg-svg,.bar-svg,.trend-svg{width:100%;max-width:560px;height:auto;display:block;margin:8px 0;}
.trend-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:10px 0;}
.trend-card{border:1px solid var(--line);border-radius:12px;padding:12px;background:#fff;}
.trend-card h4{margin:0 0 4px;font-size:14px;}
.trend-stats{display:grid;grid-template-columns:1fr 1fr;gap:3px 10px;font-size:12.5px;margin:4px 0;}
.trend-stats div{display:flex;justify-content:space-between;}
.trend-stats span{color:var(--muted);} .trend-note{font-size:12px;color:var(--muted);}
.dq-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0;}
.dq-item{border:1px solid var(--line);border-radius:10px;padding:8px 10px;
  display:flex;flex-direction:column;gap:2px;}
.dq-item span{font-size:11px;color:var(--muted);} .dq-item strong{font-size:15px;}
.dq-item.dq-warn{border-color:#f1d9b0;background:#fffaf2;}
@media (max-width:720px){
  main{padding:18px 14px 60px;}
  .stat-grid,.dq-grid{grid-template-columns:1fr 1fr;}
  .trend-grid{grid-template-columns:1fr;}
  .hero-line{font-size:20px;}
}
@media print{
  body{background:#fff;} main{max-width:none;padding:0;}
  .card{box-shadow:none;border:1px solid var(--line);break-inside:avoid;}
  .xlinks{display:none;}
  details.card>summary{display:none;}
  details.card>.details-body{margin-top:0;}
  details[open],details{}
}
"""
