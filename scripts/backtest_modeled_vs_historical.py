"""
Backtest the modeled national score against real ALLCPR history.

Where a ZIP appears in BOTH the modeled national layer and the historical
report, we can ask: does the public-data estimate actually agree with what
happened? This script matches by ZIP, computes simple correlation / linear-fit
statistics (pure Python — no numpy needed), and flags false positives (modeled
high, history low) and false negatives (modeled low, history high). It writes
``data/processed/model_backtest.json`` for the dashboard's "Model validation"
section.

It deliberately under-claims: a small overlap or a weak correlation is reported
plainly, with caveats — this validates direction, not precision.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports.report_export import (
    LATEST_REPORT_PATH,
    MODEL_BACKTEST_PATH,
    preferred_national_path,
)
from app.scoring.historical_proven_demand import compute_proven_demand_score
from app.scoring.model_calibration import compare_modeled_vs_proven
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# (label, modeled-field, historical-field)
_METRIC_PAIRS = [
    ("overall_vs_historical_score", "overall", "demand_score"),
    ("bls_vs_aha_students", "bls_demand", "aha_bls_students"),
    ("bls_vs_arc_bls_students", "bls_demand", "arc_bls_students"),
    ("cpr_vs_arc_cpr_students", "cpr_demand", "arc_cpr_students"),
    ("overall_vs_class_count", "overall", "class_count"),
    ("overall_vs_avg_students", "overall", "avg_students"),
    ("overall_vs_fill_rate", "overall", "fill_rate"),
]
_PROVEN_METRIC_PAIRS = [
    ("modeled_overall_vs_proven_demand", "overall", "proven_demand_score"),
    ("modeled_bls_vs_proven_aha_bls", "bls_demand", "proven_aha_bls_score"),
    ("modeled_bls_vs_proven_arc_bls", "bls_demand", "proven_arc_bls_score"),
    ("modeled_cpr_vs_proven_arc_cpr", "cpr_demand", "proven_arc_cpr_score"),
    ("modeled_overall_vs_avg_students", "overall", "avg_students"),
    ("modeled_overall_vs_fill_rate", "overall", "fill_rate"),
    ("modeled_overall_vs_class_count", "overall", "class_count"),
]

MODELED_HIGH_THRESHOLD = 60
MODELED_LOW_THRESHOLD = 40
PROVEN_HIGH_THRESHOLD = 60
PROVEN_LOW_THRESHOLD = 30


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs)


def _correlation_and_fit(
    pairs: List[Tuple[float, float]]
) -> Dict[str, Optional[float]]:
    """Pearson r, R², slope, intercept for (x=modeled, y=historical).

    Returns ``None`` stats when n < 2 or either variable is constant.
    """
    n = len(pairs)
    out: Dict[str, Optional[float]] = {
        "sample_size": n, "correlation": None, "r2": None,
        "slope": None, "intercept": None,
    }
    if n < 2:
        return out
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    if sxx == 0 or syy == 0:
        return out  # constant input — correlation undefined
    corr = sxy / math.sqrt(sxx * syy)
    slope = sxy / sxx
    out.update({
        "correlation": round(corr, 4),
        "r2": round(corr * corr, 4),
        "slope": round(slope, 4),
        "intercept": round(my - slope * mx, 4),
    })
    return out


def correlation_for(
    national_rows: List[Dict[str, Any]],
    historical_rows: List[Dict[str, Any]],
    *,
    field: str = "overall",
    hist_field: str = "demand_score",
    zips: Optional[List[str]] = None,
) -> Dict[str, Optional[float]]:
    """Correlation/fit of one modeled field vs one historical field.

    Optionally restricted to ``zips`` — used to compare baseline vs enriched
    scores on the SAME ZIP subset (the Phase-2 enrichment experiment).
    """
    modeled = {str(r.get("zip")): r for r in national_rows if r.get("zip")}
    historical = {str(r.get("zip")): r for r in historical_rows if r.get("zip")}
    keys = set(modeled) & set(historical)
    if zips is not None:
        keys &= set(zips)
    pairs: List[Tuple[float, float]] = []
    for z in keys:
        mv = _num(modeled[z].get(field))
        hv = _num(historical[z].get(hist_field))
        if mv is not None and hv is not None:
            pairs.append((mv, hv))
    return _correlation_and_fit(pairs)


def classify_validation_case(modeled_overall: Any, proven_demand: Any) -> str:
    """Classify one ZIP's modeled-vs-proven demand relationship.

    History is never imputed. ZIPs without a real historical demand score are
    explicitly outside calibration and should remain pure modeled estimates.
    """
    modeled_value = _num(modeled_overall)
    proven_value = _num(proven_demand)
    if modeled_value is None or proven_value is None:
        return "no_overlap"
    if modeled_value >= MODELED_HIGH_THRESHOLD and proven_value < PROVEN_LOW_THRESHOLD:
        return "false_positive"
    if modeled_value < MODELED_LOW_THRESHOLD and proven_value >= PROVEN_HIGH_THRESHOLD:
        return "false_negative"
    if modeled_value >= MODELED_HIGH_THRESHOLD and proven_value >= PROVEN_HIGH_THRESHOLD:
        return "confirmed_high"
    if modeled_value < MODELED_LOW_THRESHOLD and proven_value < PROVEN_LOW_THRESHOLD:
        return "confirmed_low"
    return "mixed"


def _validation_reason(case: str) -> str:
    return {
        "false_positive": (
            "Modeled score is high, but real ALLCPR student outcomes were low."
        ),
        "false_negative": (
            "Modeled score is low, but real ALLCPR student outcomes were strong."
        ),
        "confirmed_high": (
            "Modeled score and real ALLCPR student outcomes both indicate strength."
        ),
        "confirmed_low": (
            "Modeled score and real ALLCPR student outcomes both indicate weakness."
        ),
        "mixed": (
            "Modeled score and real ALLCPR student outcomes are not clearly aligned."
        ),
    }.get(case, "No overlapping real ALLCPR history for calibration.")


def build_validation_rows(
    national_rows: List[Dict[str, Any]],
    historical_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return side-by-side modeled vs proven demand rows for ZIP overlap only."""
    modeled = {str(r.get("zip")): r for r in national_rows if r.get("zip")}
    historical = {str(r.get("zip")): r for r in historical_rows if r.get("zip")}
    rows: List[Dict[str, Any]] = []
    for z in sorted(set(modeled) & set(historical)):
        m = modeled[z]
        h = historical[z]
        calibration = compare_modeled_vs_proven(m, h)
        proven_score = (
            _num(calibration.get("proven_demand_score"))
            if calibration.get("proven_demand_score") is not None
            else _num(h.get("demand_score"))
        )
        case = classify_validation_case(m.get("overall"), proven_score)
        rows.append({
            "zip": z,
            "modeled_overall": _num(m.get("overall")),
            "modeled_bls_demand": _num(m.get("bls_demand")),
            "modeled_cpr_demand": _num(m.get("cpr_demand")),
            "historical_demand_score": _num(h.get("demand_score")),
            "proven_demand_score": proven_score,
            "proven_total_students": _num(h.get("total_students")),
            "proven_class_count": _num(h.get("class_count")),
            "proven_avg_students": _num(h.get("avg_students")),
            "proven_fill_rate": _num(h.get("fill_rate")),
            "validation_case": case,
            "validation_reason": _validation_reason(case),
            "historical_confidence": calibration.get("historical_confidence"),
            "best_historical_course": calibration.get("best_historical_course"),
            "historical_course_mix": calibration.get("historical_course_mix"),
            "model_error": calibration.get("model_error"),
            "model_agreement": calibration.get("model_agreement"),
            "calibration_note": calibration.get("calibration_note"),
        })
    rows.sort(key=lambda r: (
        r["validation_case"] not in {"false_positive", "false_negative"},
        -(r.get("modeled_overall") or 0),
        str(r.get("zip") or ""),
    ))
    return rows


def compute_backtest(
    national_rows: List[Dict[str, Any]],
    historical_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match by ZIP and compute the backtest payload (pure, no IO)."""
    modeled = {str(r.get("zip")): r for r in national_rows if r.get("zip")}
    historical = {str(r.get("zip")): r for r in historical_rows if r.get("zip")}
    common = sorted(set(modeled) & set(historical))

    historical_scored = []
    for row in historical_rows:
        historical_scored.append({**row, **compute_proven_demand_score(row)})

    metrics: Dict[str, Any] = {}
    for label, m_field, h_field in (*_METRIC_PAIRS, *_PROVEN_METRIC_PAIRS):
        pairs: List[Tuple[float, float]] = []
        hist_source = historical_scored if h_field.startswith("proven_") else historical_rows
        historical_by_zip = {
            str(r.get("zip")): r for r in hist_source if r.get("zip")
        }
        for z in common:
            mv = _num(modeled[z].get(m_field))
            hv = _num(historical_by_zip.get(z, {}).get(h_field))
            if mv is not None and hv is not None:
                pairs.append((mv, hv))
        metrics[label] = _correlation_and_fit(pairs)

    validation_rows = build_validation_rows(national_rows, historical_rows)

    # Top modeled ZIPs (by overall) with their actual historical outcomes.
    top = sorted(common, key=lambda z: _num(modeled[z].get("overall")) or 0,
                 reverse=True)[:10]
    top_modeled_zips = [{
        "zip": z,
        "modeled_overall": _num(modeled[z].get("overall")),
        "historical_demand_score": _num(historical[z].get("demand_score")),
        "class_count": _num(historical[z].get("class_count")),
        "total_students": _num(historical[z].get("total_students")),
    } for z in top]

    false_positives = [
        row for row in validation_rows
        if row["validation_case"] == "false_positive"
    ]
    false_negatives = [
        row for row in validation_rows
        if row["validation_case"] == "false_negative"
    ]
    confirmed_high = [
        row for row in validation_rows
        if row["validation_case"] == "confirmed_high"
    ]
    confirmed_low = [
        row for row in validation_rows
        if row["validation_case"] == "confirmed_low"
    ]
    overpredicted_zips = [
        row for row in validation_rows
        if row.get("model_agreement") in {"model_overpredicts", "test_carefully"}
    ]
    underpredicted_zips = [
        row for row in validation_rows
        if row.get("model_agreement") == "model_underpredicts"
    ]
    hidden_opportunity_zips = [
        row for row in validation_rows
        if row.get("model_agreement") == "hidden_opportunity"
    ]
    agreement_summary: Dict[str, int] = {}
    for row in validation_rows:
        key = str(row.get("model_agreement") or "unknown")
        agreement_summary[key] = agreement_summary.get(key, 0) + 1

    notes = [
        "Overlap = ZIPs present in BOTH the modeled layer and real history.",
        "History is used for calibration/validation only; it is not blended into national ZIPs without history.",
        "Modeled demand and proven ALLCPR outcomes should be shown side by side.",
        "Correlation validates DIRECTION, not precision; do not over-read it.",
    ]
    if len(common) < 30:
        notes.append(f"Small overlap (n={len(common)}) — treat statistics as "
                     f"indicative only.")
    if not common:
        notes.append("No ZIP overlap — cannot validate the model yet.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(common),
        "metrics": metrics,
        "calibration_thresholds": {
            "modeled_high": MODELED_HIGH_THRESHOLD,
            "modeled_low": MODELED_LOW_THRESHOLD,
            "proven_high": PROVEN_HIGH_THRESHOLD,
            "proven_low": PROVEN_LOW_THRESHOLD,
        },
        "calibration_summary": {
            "overlap_zips": len(common),
            "false_positive_count": len(false_positives),
            "false_negative_count": len(false_negatives),
            "confirmed_high_count": len(confirmed_high),
            "confirmed_low_count": len(confirmed_low),
        },
        "modeled_vs_proven": validation_rows,
        "top_modeled_zips": top_modeled_zips,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "overpredicted_zips": overpredicted_zips,
        "underpredicted_zips": underpredicted_zips,
        "hidden_opportunity_zips": hidden_opportunity_zips,
        "model_agreement_summary": agreement_summary,
        "notes": notes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--national", default="",
                    help="National modeled JSON (default: enriched if present, "
                         "else baseline).")
    ap.add_argument("--report", default=str(LATEST_REPORT_PATH),
                    help="Historical latest_report.json.")
    ap.add_argument("--output", default=str(MODEL_BACKTEST_PATH))
    args = ap.parse_args(argv)

    nat_path = Path(args.national) if args.national else preferred_national_path()
    rep_path = Path(args.report)
    if not nat_path.exists():
        logger.error(f"National layer not found: {nat_path}. "
                     f"Run build_national_demand.py first.")
        return 1
    if not rep_path.exists():
        logger.error(f"Report not found: {rep_path}. "
                     f"Run generate_html_report.py first.")
        return 1

    national = json.loads(nat_path.read_text(encoding="utf-8"))
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    result = compute_backtest(national.get("rows") or [],
                              report.get("zip_demand") or [])

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info(f"Backtest: {result['sample_size']} overlapping ZIPs → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
