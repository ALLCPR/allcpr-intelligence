"""
Reusable JSON export for the web dashboard.

The website (``web_app.py``) does NOT re-run the scoring pipeline. Instead it
reads a single pre-generated file — ``data/processed/latest_report.json`` —
produced from the very same report payload that drives the HTML report. This
module is the bridge: it flattens the existing report context (executive
verdict, ZIP demand rows, candidates) into a stable, snake_case,
frontend-friendly shape.

Design rules (see boss spec):
  * Reuse existing values; never invent business numbers.
  * Missing numeric fields default to 0 or null — never crash.
  * Keep raw historical counts; let the client normalize heat values.
  * Course-specific "heat" falls back to real historical student counts when
    no course-specific score exists.

Public API:
  * ``build_latest_report_payload(report_payload) -> dict``
  * ``write_latest_report_json(report_payload, output_path=...) -> Path``
  * ``load_latest_report_json(path=...) -> dict``
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import PROCESSED_DIR

LATEST_REPORT_PATH = PROCESSED_DIR / "latest_report.json"
# National modeled-demand layer (built offline by scripts/build_national_demand.py).
NATIONAL_DEMAND_PATH = PROCESSED_DIR / "national_demand.json"
# Phase-2 enriched national layer (scripts/enrich_top_zips.py). Preferred when present.
NATIONAL_DEMAND_ENRICHED_PATH = PROCESSED_DIR / "national_demand_enriched.json"
# Model backtest (scripts/backtest_modeled_vs_historical.py).
MODEL_BACKTEST_PATH = PROCESSED_DIR / "model_backtest.json"

# Course-type keys shared by the ZIP demand rows and the course-performance
# section. Order is the canonical display order used by the dashboard selector.
COURSE_KEYS = ("aha_bls", "arc_bls", "arc_cpr")
COURSE_LABELS = {
    "overall": "Overall",
    "aha_bls": "AHA BLS",
    "arc_bls": "ARC BLS",
    "arc_cpr": "ARC CPR",
}

_ZIP_RE = re.compile(r"\b(\d{5})\b")


# --------------------------------------------------------------------------- #
# Small, defensive coercion helpers
# --------------------------------------------------------------------------- #
def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Coerce to float; return ``default`` on anything non-numeric/NaN."""
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def _num0(value: Any) -> float:
    """Coerce to float, defaulting missing/invalid to 0.0 (for heat values)."""
    out = _num(value, 0.0)
    return out if out is not None else 0.0


def _int0(value: Any) -> int:
    out = _num(value, 0.0)
    return int(out) if out is not None else 0


def _str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_zip(*candidates: Any) -> Optional[str]:
    for cand in candidates:
        if cand is None:
            continue
        match = _ZIP_RE.search(str(cand))
        if match:
            return match.group(1)
    return None


# --------------------------------------------------------------------------- #
# Executive summary
# --------------------------------------------------------------------------- #
def _best_course_label(course_performance: Dict[str, Any]) -> Optional[str]:
    """Course type with the most historical students across the area."""
    course_types = (course_performance or {}).get("course_types") or []
    best_label: Optional[str] = None
    best_total = -1.0
    for ct in course_types:
        total = _num0(ct.get("total_students"))
        if total > best_total:
            best_total = total
            best_label = _str(ct.get("label")) or COURSE_LABELS.get(
                str(ct.get("course_type")), _str(ct.get("course_type"))
            )
    if best_total <= 0:
        return None
    return best_label


def _data_confidence_note(payload: Dict[str, Any], zip_count: int,
                          total_classes: Optional[float]) -> Optional[str]:
    """A short, honest statement of how much history backs the report."""
    if total_classes and zip_count:
        return (
            f"Based on {int(total_classes)} historical classes across "
            f"{zip_count} ZIP codes. Scores are decision-support, not "
            f"guaranteed enrollment predictions."
        )
    return None


def build_executive_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    interp = payload.get("report_interpretation") or {}
    ev = interp.get("executive_verdict") or {}
    candidates = payload.get("candidates") or []
    context = payload.get("context") or {}
    course_perf = context.get("course_performance") or {}

    # Scores come from the best (rank-1) candidate's scored block when present;
    # ev only carries a formatted score_line string.
    best_scored: Dict[str, Any] = {}
    if candidates:
        best = min(candidates, key=lambda c: _num(c.get("rank"), 9_999) or 9_999)
        best_scored = best.get("scored") or {}

    area_score = _num(best_scored.get("area_score"))
    if area_score is None:
        area_score = _num(best_scored.get("site_score"))
    site_score = _num(best_scored.get("site_score"))

    ai_summary = context.get("ai_summary") or {}
    long_summary = _str(ai_summary.get("text"))

    zip_report = context.get("zip_demand_report") or {}
    note = _data_confidence_note(
        payload,
        _int0(zip_report.get("total_zips")) or len(zip_report.get("rows") or []),
        _num(zip_report.get("total_classes"))
        or _num(course_perf.get("total_classes")),
    )

    return {
        "best_area": _str(ev.get("best_candidate")),
        "recommendation": _str(ev.get("executive_state")),
        "area_score": area_score,
        "site_score": site_score,
        "confidence": _str(ev.get("confidence")),
        "verdict": _str(ev.get("verdict")),
        "expansion_readiness": _str(ev.get("expansion_readiness")),
        "best_course": _best_course_label(course_perf),
        "why_it_matters": _str(ev.get("why_it_matters")),
        "biggest_risk": _str(ev.get("biggest_risk")),
        "best_strategy": _str(ev.get("best_strategy")),
        "before_leasing": _str(ev.get("before_leasing")),
        "next_actions": [s for s in (interp.get("next_actions") or []) if s],
        "long_summary": long_summary,
        "data_confidence_note": note,
    }


# --------------------------------------------------------------------------- #
# ZIP demand rows
# --------------------------------------------------------------------------- #
def _zip_confidence(classes: int) -> str:
    """Deterministic confidence in the *historical signal* for a ZIP."""
    if classes >= 20:
        return "High"
    if classes >= 8:
        return "Medium"
    if classes >= 1:
        return "Low"
    return "None"


def _zip_recommendation(demand_score: float, activity: float) -> str:
    """Overall (demand-score based) recommendation. The dashboard recomputes a
    course-specific version client-side; this is the report-level default."""
    if demand_score >= 60 and activity > 0:
        return "Strong test area"
    if demand_score >= 60:
        return "Possible opportunity, needs demand test"
    if activity > 0:
        return "Historical activity exists; review before ignoring"
    return "Lower priority"


def _zip_reason(demand_score: float, activity: float) -> str:
    hi_score = demand_score >= 60
    hi_activity = activity > 0
    if hi_score and hi_activity:
        return "This ZIP has both a strong demand score and real course activity."
    if hi_score and not hi_activity:
        return ("This ZIP has demand signals but limited historical class "
                "activity.")
    if not hi_score and hi_activity:
        return ("This ZIP has historical course activity even though the model "
                "score is lower.")
    return "This ZIP is lower priority based on current score and history."


def _zip_best_course(aha: float, arc_bls: float, arc_cpr: float) -> Optional[str]:
    pairs = (("aha_bls", aha), ("arc_bls", arc_bls), ("arc_cpr", arc_cpr))
    label, value = max(pairs, key=lambda p: p[1])
    if value <= 0:
        return None
    return COURSE_LABELS[label]


def build_zip_demand_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    context = payload.get("context") or {}
    zip_report = context.get("zip_demand_report") or {}
    raw_rows = zip_report.get("rows") or []

    rows: List[Dict[str, Any]] = []
    for raw in raw_rows:
        zip_code = _str(raw.get("zip"))
        if not zip_code:
            continue
        lat = _num(raw.get("lat"))
        lng = _num(raw.get("lng"))
        centroid_present = bool(raw.get("centroid_present"))
        # Some rows carry coords without the flag, and vice versa; trust coords.
        missing_centroid = not (centroid_present and lat is not None
                                and lng is not None)

        demand_score = _num0(raw.get("demand_score"))
        classes = _int0(raw.get("classes"))
        aha = _num0(raw.get("aha_bls_students"))
        arc_bls = _num0(raw.get("arc_bls_students"))
        arc_cpr = _num0(raw.get("arc_cpr_students"))
        total_students = _num(raw.get("total_students"))
        if total_students is None:
            total_students = aha + arc_bls + arc_cpr
        activity = aha + arc_bls + arc_cpr

        rows.append({
            "zip": zip_code,
            "lat": lat,
            "lng": lng,
            "demand_score": demand_score,
            "class_count": classes,
            "arc_cpr_students": arc_cpr,
            "arc_bls_students": arc_bls,
            "aha_bls_students": aha,
            # No separate "people" counts exist upstream; expose null so the
            # client fallback chain is well-defined.
            "arc_cpr_people": None,
            "arc_bls_people": None,
            "aha_bls_people": None,
            "avg_students": _num0(raw.get("avg_students")),
            "total_students": total_students,
            "fill_rate": _num0(raw.get("fill_rate")),
            "confidence": _zip_confidence(classes),
            "recommendation": _zip_recommendation(demand_score, activity),
            "reason": _zip_reason(demand_score, activity),
            "best_course": _zip_best_course(aha, arc_bls, arc_cpr),
            "latest_class": _str(raw.get("latest_class_date")),
            "missing_centroid": missing_centroid,
            "centroid_source": _str(raw.get("centroid_source")),
            "city": _str(raw.get("city")),
            "state": _str(raw.get("state")),
            "strength": _str(raw.get("strength")),
            "month_span": _num(raw.get("month_span")),
            "course_scores": {
                "overall": demand_score,
                "aha_bls": aha,
                "arc_bls": arc_bls,
                "arc_cpr": arc_cpr,
            },
        })

    # Rank by demand score (desc); stable tie-break on ZIP for determinism.
    rows.sort(key=lambda r: (-r["demand_score"], r["zip"]))
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
_TIER_VERDICT = {
    "A": "Strong location",
    "B": "Promising — validate first",
    "C": "Mixed — needs more data",
    "D": "Not recommended",
    "F": "Avoid",
}


def _candidate_confidence(scored: Dict[str, Any]) -> Optional[str]:
    sub = scored.get("sub_scores") or {}
    conf = _num(sub.get("confidence_score"))
    if conf is None:
        conf = _num(scored.get("confidence_score_adjusted"))
    if conf is None:
        return None
    if conf >= 80:
        band = "Very high"
    elif conf >= 65:
        band = "High"
    elif conf >= 45:
        band = "Moderate"
    else:
        band = "Low"
    return f"{band} ({conf:.0f}/100)"


def build_candidate_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = payload.get("candidates") or []
    interp_report = payload.get("report_interpretation") or {}
    ev = interp_report.get("executive_verdict") or {}
    course_perf = (payload.get("context") or {}).get("course_performance") or {}
    best_course = _best_course_label(course_perf)
    report_next_actions = [s for s in (interp_report.get("next_actions") or [])
                           if s]
    best_rank = min((_num(c.get("rank"), 9_999) or 9_999 for c in candidates),
                    default=None)

    rows: List[Dict[str, Any]] = []
    for cand in candidates:
        profile = cand.get("profile") or {}
        scored = cand.get("scored") or {}
        interp = cand.get("interpretation") or {}
        anchor = profile.get("anchor") or {}
        rank = _num(cand.get("rank"))
        is_best = rank is not None and rank == best_rank

        address = _str(anchor.get("formatted_address"))
        warnings = interp.get("warnings") or []
        readiness = (interp.get("expansion_readiness") or {}).get("readiness")
        tier = str(scored.get("tier") or "").upper()

        rows.append({
            "name": (_str(profile.get("area_display_name"))
                     or _str(anchor.get("name"))
                     or _str(profile.get("candidate_name"))),
            "address": address,
            "city": _str(profile.get("city")),
            "state": _str(profile.get("state")),
            "zip": _parse_zip(address, profile.get("comparison_area")),
            "lat": _num(profile.get("latitude")),
            "lng": _num(profile.get("longitude")),
            "area_score": _num(scored.get("area_score")),
            "site_score": _num(scored.get("site_score")),
            "recommendation": _str(scored.get("executive_state")),
            "best_course": best_course if is_best else None,
            "confidence": _candidate_confidence(scored),
            "verdict": _str(scored.get("tier_label")) or _TIER_VERDICT.get(tier),
            "expansion_readiness": _str(readiness),
            "why_it_matters": _str(ev.get("why_it_matters")) if is_best else None,
            "biggest_risk": _str(warnings[0]) if warnings else (
                _str(ev.get("biggest_risk")) if is_best else None),
            "next_actions": report_next_actions if is_best else [],
            "anchor_type": (_str(anchor.get("category"))
                            or _str((anchor.get("source_query")))),
            "source": _str(profile.get("candidate_source")),
        })
    return rows


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def build_metadata(payload: Dict[str, Any], zip_rows: List[Dict[str, Any]],
                   candidate_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = payload.get("candidates") or []
    data_sources: List[str] = []
    seen = set()
    for cand in candidates:
        for name in (cand.get("profile") or {}).get("source_names") or []:
            if name and name not in seen:
                seen.add(name)
                data_sources.append(name)

    missing_centroid_count = sum(1 for r in zip_rows if r["missing_centroid"])
    candidates_without_coords = sum(
        1 for c in candidate_rows if c["lat"] is None or c["lng"] is None
    )

    warnings: List[str] = []
    if missing_centroid_count:
        warnings.append(
            f"{missing_centroid_count} ZIP(s) lack centroid coordinates and "
            f"cannot be located on the map yet."
        )
    if candidates_without_coords:
        warnings.append(
            f"{candidates_without_coords} candidate(s) lack coordinates and "
            f"are not drawn on the map."
        )

    notes = [
        "Scores are decision-support, not guaranteed enrollment predictions.",
        "Course heat modes use historical student counts where available.",
        "A lease decision still requires rent, parking, classroom "
        "availability, zoning/use, and competitor-schedule validation.",
        "Do not sign a lease based on score alone — test first.",
    ]

    return {
        "data_sources": data_sources,
        "warnings": warnings,
        "missing_centroid_count": missing_centroid_count,
        "zip_count": len(zip_rows),
        "candidate_count": len(candidate_rows),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Top-level assembly + IO
# --------------------------------------------------------------------------- #
def build_latest_report_payload(report_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the in-memory report payload into the dashboard JSON shape."""
    report_payload = report_payload or {}
    context = report_payload.get("context") or {}

    cities = context.get("cities") or []
    city = cities[0] if isinstance(cities, list) and cities else _str(
        context.get("city"))

    zip_rows = build_zip_demand_rows(report_payload)
    candidate_rows = build_candidate_rows(report_payload)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": _str(context.get("mode")),
        "city": city,
        "executive_summary": build_executive_summary(report_payload),
        "zip_demand": zip_rows,
        "candidates": candidate_rows,
        "metadata": build_metadata(report_payload, zip_rows, candidate_rows),
    }


def write_latest_report_json(
    report_payload: Dict[str, Any],
    output_path: Any = LATEST_REPORT_PATH,
) -> Path:
    """Build and persist ``latest_report.json``; returns the written path."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_latest_report_payload(report_payload)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return out


def load_latest_report_json(path: Any = LATEST_REPORT_PATH) -> Dict[str, Any]:
    """Load a previously written ``latest_report.json``.

    Raises ``FileNotFoundError`` if it does not exist — callers (the web API)
    translate that into a helpful, user-facing error message.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def preferred_national_path() -> Path:
    """Prefer the Phase-2 enriched national file when it exists, else baseline."""
    return (NATIONAL_DEMAND_ENRICHED_PATH
            if NATIONAL_DEMAND_ENRICHED_PATH.exists()
            else NATIONAL_DEMAND_PATH)


def load_national_demand_json(path: Any = None) -> Dict[str, Any]:
    """Load the offline-built national modeled-demand layer.

    With ``path=None`` it auto-selects the enriched file when present, else the
    baseline. Raises ``FileNotFoundError`` when neither exists — the web API
    turns that into a helpful "run build_national_demand" message.
    """
    p = Path(path) if path is not None else preferred_national_path()
    if not p.exists():
        raise FileNotFoundError(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def load_model_backtest_json(path: Any = MODEL_BACKTEST_PATH) -> Dict[str, Any]:
    """Load the model backtest output. Raises ``FileNotFoundError`` if absent."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    return json.loads(p.read_text(encoding="utf-8"))
