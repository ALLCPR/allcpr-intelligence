#!/usr/bin/env python3
"""Compare v2.0 Opportunity Score with v2.1 Site Priority Score.

Offline-only. Reads generated dashboard artifacts, computes v2.1 side-by-side,
and prints rank/decision diagnostics without mutating source data.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.reports.commercial_validation import (
    COMMERCIAL_VALIDATION_FILE,
    load_commercial_summaries,
)
from app.scoring.site_priority_score import annotate_site_priority_scores

PROCESSED_DIR = ROOT / "data" / "processed"
ZIP_DETAILS_JSONL = PROCESSED_DIR / "zip_details.jsonl"
MODEL_BACKTEST = PROCESSED_DIR / "model_backtest.json"
DEFAULT_JSON_OUT = PROCESSED_DIR / "score_formula_v20_vs_v21_comparison.json"
SPOT_ZIPS = ("95112", "07030", "10016")


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _old_score(row: Dict[str, Any]) -> Optional[float]:
    if row.get("old_opportunity_score") is not None:
        return _num(row.get("old_opportunity_score"))
    return _num(row.get("overall") if row.get("overall") is not None
                else row.get("overall_score"))


def _new_score(row: Dict[str, Any]) -> Optional[float]:
    return _num(row.get("final_site_priority_score"))


def _is_enriched(row: Dict[str, Any]) -> bool:
    return row.get("tier") == "enriched" or bool(row.get("enrichment_tier"))


def _bay_area(row: Dict[str, Any]) -> bool:
    lat = _num(row.get("lat"))
    lng = _num(row.get("lng") if row.get("lng") is not None else row.get("lon"))
    if lat is None or lng is None:
        return False
    return 36.75 <= lat <= 38.65 and -123.25 <= lng <= -121.15


def _load_jsonl(path: Path,
                commercial_by_zip: Dict[str, Dict[str, Any]],
                backtest_by_zip: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            zip_code = str(row.get("zip")).zfill(5)
            if zip_code in backtest_by_zip:
                row.update(backtest_by_zip[zip_code])
            if zip_code in commercial_by_zip:
                row["commercial"] = commercial_by_zip[zip_code]
            rows.append(annotate_site_priority_scores(row))
    return rows


def _load_backtest(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("zip")).zfill(5): row
        for row in data.get("modeled_vs_proven") or []
        if row.get("zip")
    }


def _ranked(rows: Sequence[Dict[str, Any]], key) -> List[Dict[str, Any]]:
    return sorted(
        [row for row in rows if key(row) is not None],
        key=lambda row: (-(key(row) or 0.0), str(row.get("zip") or "")),
    )


def _ranks(rows: Sequence[Dict[str, Any]], key) -> Dict[str, int]:
    return {
        str(row.get("zip")).zfill(5): i
        for i, row in enumerate(_ranked(rows, key), start=1)
    }


def _brief(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "zip": str(row.get("zip")).zfill(5),
        "old_opportunity_score": _old_score(row),
        "final_site_priority_score": _new_score(row),
        "market_demand_score": row.get("market_demand_score"),
        "validation_evidence_score": row.get("validation_evidence_score"),
        "commercial_feasibility_score": row.get("commercial_feasibility_score"),
        "commercial_feasibility_status": row.get("commercial_feasibility_status"),
        "competition_saturation_penalty": row.get("competition_saturation_penalty"),
        "competition_risk_level": row.get("competition_risk_level"),
        "site_priority_decision": row.get("site_priority_decision"),
    }


def _pearson(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return None
    return round(num / (dx * dy), 4)


def _hist_metric(rows_by_zip: Dict[str, Dict[str, Any]],
                 backtest: Dict[str, Dict[str, Any]],
                 metric: str,
                 score_key) -> Optional[float]:
    pairs: List[Tuple[float, float]] = []
    for zip_code, bt in backtest.items():
        row = rows_by_zip.get(zip_code)
        if not row:
            continue
        score = score_key(row)
        outcome = _num(bt.get(metric))
        if score is not None and outcome is not None:
            pairs.append((score, outcome))
    return _pearson(pairs)


def _rank_changes(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    old_ranks = _ranks(rows, _old_score)
    new_ranks = _ranks(rows, _new_score)
    out = []
    for row in rows:
        z = str(row.get("zip")).zfill(5)
        if z not in old_ranks or z not in new_ranks:
            continue
        out.append({
            **_brief(row),
            "old_rank": old_ranks[z],
            "new_rank": new_ranks[z],
            "rank_change": old_ranks[z] - new_ranks[z],
        })
    return out


def _commercial_missing(row: Dict[str, Any]) -> bool:
    return not bool(row.get("commercial_feasibility_confirmed"))


def _ready_without_commercial(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        row for row in rows
        if _commercial_missing(row)
        and "ready" in str(row.get("site_priority_decision") or "").lower()
    ]


def _print_table(title: str, rows: Sequence[Dict[str, Any]], *,
                 limit: int = 25,
                 rank_fields: bool = False) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for i, row in enumerate(rows[:limit], start=1):
        b = _brief(row)
        prefix = f"{i:>2}. "
        if rank_fields:
            prefix = (
                f"{i:>2}. old #{row['old_rank']} -> new #{row['new_rank']} "
                f"({row['rank_change']:+}) "
            )
        print(
            f"{prefix}{b['zip']} old={b['old_opportunity_score']} "
            f"new={b['final_site_priority_score']} market={b['market_demand_score']} "
            f"validation={b['validation_evidence_score']} "
            f"commercial={b['commercial_feasibility_status']} "
            f"risk={b['competition_risk_level']} "
            f"decision={b['site_priority_decision']}"
        )


def build_comparison(rows: List[Dict[str, Any]],
                     backtest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rows_by_zip = {str(row.get("zip")).zfill(5): row for row in rows}
    enriched = [row for row in rows if _is_enriched(row)]
    bay = [row for row in rows if _bay_area(row)]
    bay_top100_old = _ranked(bay, _old_score)[:100]
    changes = _rank_changes(rows)
    changes_by_zip = {row["zip"]: row for row in changes}
    risers = sorted(changes, key=lambda r: r["rank_change"], reverse=True)
    fallers = sorted(changes, key=lambda r: r["rank_change"])

    historical_zips = [rows_by_zip[z] for z in backtest if z in rows_by_zip]
    hidden = [
        changes_by_zip[z] for z, bt in backtest.items()
        if z in changes_by_zip
        and bt.get("model_agreement") in {"hidden_opportunity", "model_underpredicts"}
    ]
    hidden_improved = sum(1 for row in hidden if row["rank_change"] > 0)

    correlations = {}
    for metric in (
        "proven_demand_score",
        "proven_class_count",
        "proven_avg_students",
        "proven_fill_rate",
    ):
        correlations[f"old_vs_{metric}"] = _hist_metric(
            rows_by_zip, backtest, metric, _old_score)
        correlations[f"new_vs_{metric}"] = _hist_metric(
            rows_by_zip, backtest, metric, _new_score)

    known_strong = [
        changes_by_zip[z] for z, bt in backtest.items()
        if z in changes_by_zip and (_num(bt.get("proven_demand_score")) or 0) >= 70
    ]
    known_strong_old = [row["old_rank"] for row in known_strong]
    known_strong_new = [row["new_rank"] for row in known_strong]

    top_new = _ranked(rows, _new_score)[:25]
    top_new_places_only = [
        row for row in top_new
        if "places_signal_not_enough_without_public_demand"
        in (row.get("site_priority_risk_flags") or [])
    ]
    competition_heavy = [
        row for row in rows
        if (_num(row.get("competitor_count")) or 0) >= 20
    ]
    competition_heavy_missing_risk = [
        row for row in competition_heavy
        if row.get("competition_risk_level") != "saturated_unless_differentiated"
    ]

    summary = {
        "row_count": len(rows),
        "enriched_zip_count": len(enriched),
        "bay_area_zip_count": len(bay),
        "bay_area_top100_old_count": len(bay_top100_old),
        "historical_backtest_zip_count": len(historical_zips),
        "commercial_missing_capped_count": sum(
            1 for row in rows
            if _commercial_missing(row)
            and row.get("site_priority_score_status") == "provisional"
        ),
        "competition_heavy_count": len(competition_heavy),
        "hidden_or_underpredicted_count": len(hidden),
        "hidden_or_underpredicted_improved_rank_count": hidden_improved,
        "known_strong_historical_count": len(known_strong),
        "known_strong_old_median_rank": (
            round(median(known_strong_old), 1) if known_strong_old else None
        ),
        "known_strong_new_median_rank": (
            round(median(known_strong_new), 1) if known_strong_new else None
        ),
        "correlations": correlations,
        "production_gates": {
            "commercial_missing_not_ready": len(_ready_without_commercial(rows)) == 0,
            "no_places_only_top25": len(top_new_places_only) == 0,
            "competition_heavy_has_saturation_risk": (
                len(competition_heavy_missing_risk) == 0
            ),
        },
    }

    return {
        "summary": summary,
        "spot_zips": {
            z: changes_by_zip.get(z) or _brief(rows_by_zip[z])
            for z in SPOT_ZIPS
            if z in rows_by_zip
        },
        "top25_old_v20": [_brief(row) for row in _ranked(rows, _old_score)[:25]],
        "top25_new_v21": [_brief(row) for row in top_new],
        "top25_old_bay_area": [_brief(row) for row in bay_top100_old[:25]],
        "top25_old_enriched": [_brief(row) for row in _ranked(enriched, _old_score)[:25]],
        "top25_new_enriched": [_brief(row) for row in _ranked(enriched, _new_score)[:25]],
        "biggest_risers": risers[:25],
        "biggest_fallers": fallers[:25],
        "high_validation_moderate_old": [
            _brief(row) for row in rows
            if (_num(row.get("validation_evidence_score")) or 0) >= 75
            and 40 <= (_old_score(row) or -1) <= 65
        ][:25],
        "high_demand_high_saturation": [
            _brief(row) for row in rows
            if (_num(row.get("market_demand_score")) or 0) >= 65
            and (_num(row.get("competition_saturation_penalty")) or 0) >= 14
        ][:25],
        "commercial_missing_capped": [
            _brief(row) for row in rows
            if _commercial_missing(row)
            and row.get("site_priority_score_status") == "provisional"
        ][:25],
        "ready_without_commercial": [_brief(row) for row in _ready_without_commercial(rows)],
    }


def print_comparison(result: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    summary = result["summary"]
    print("v2.0 vs v2.1 score comparison")
    print("============================")
    print(f"Rows scored: {summary['row_count']:,}")
    print(f"Enriched ZIPs: {summary['enriched_zip_count']:,}")
    print(f"Bay Area ZIPs: {summary['bay_area_zip_count']:,}")
    print(f"Historical backtest ZIPs matched: {summary['historical_backtest_zip_count']:,}")
    print(f"Commercial-missing provisional caps: {summary['commercial_missing_capped_count']:,}")
    print(f"Competition-heavy ZIPs: {summary['competition_heavy_count']:,}")

    print("\nProduction gates")
    print("----------------")
    gates = summary["production_gates"]
    for key, passed in gates.items():
        print(f"{'PASS' if passed else 'WARN'} {key}")
    if summary["known_strong_historical_count"]:
        print(
            "Known strong historical median rank: "
            f"old={summary['known_strong_old_median_rank']} "
            f"new={summary['known_strong_new_median_rank']}"
        )
    if summary["hidden_or_underpredicted_count"]:
        print(
            "Hidden/underpredicted historical ZIPs with improved rank: "
            f"{summary['hidden_or_underpredicted_improved_rank_count']} / "
            f"{summary['hidden_or_underpredicted_count']}"
        )

    print("\nHistorical correlations")
    print("-----------------------")
    for key, value in summary["correlations"].items():
        print(f"{key}: {value}")

    spot_rows = [row for row in rows if str(row.get("zip")).zfill(5) in SPOT_ZIPS]
    _print_table("Spot ZIPs", spot_rows, limit=10)
    _print_table("Top 25 old v2.0 by Opportunity Score", _ranked(rows, _old_score))
    _print_table("Top 25 new v2.1 by Final Site Priority Score", _ranked(rows, _new_score))
    _print_table("Biggest risers", result["biggest_risers"], rank_fields=True)
    _print_table("Biggest fallers", result["biggest_fallers"], rank_fields=True)
    _print_table("High validation, moderate old score",
                 result["high_validation_moderate_old"])
    _print_table("High demand with high saturation penalty",
                 result["high_demand_high_saturation"])
    _print_table("Commercial-missing capped ZIPs",
                 result["commercial_missing_capped"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details-jsonl", type=Path, default=ZIP_DETAILS_JSONL)
    parser.add_argument("--model-backtest", type=Path, default=MODEL_BACKTEST)
    parser.add_argument("--commercial-csv", type=Path,
                        default=COMMERCIAL_VALIDATION_FILE)
    parser.add_argument("--json-output", type=Path, default=None,
                        help="Optional path for machine-readable comparison JSON.")
    args = parser.parse_args(argv)

    if not args.details_jsonl.exists():
        raise SystemExit(f"ZIP details JSONL not found: {args.details_jsonl}")
    backtest = _load_backtest(args.model_backtest)
    commercial_by_zip = load_commercial_summaries(args.commercial_csv)
    rows = _load_jsonl(args.details_jsonl, commercial_by_zip, backtest)
    result = build_comparison(rows, backtest)
    print_comparison(result, rows)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nWrote JSON comparison: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
