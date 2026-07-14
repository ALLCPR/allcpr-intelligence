"""
QA the national modeled-demand layer before trusting a full US build.

This script is intentionally offline/local with respect to paid enrichment:
it reads the generated modeled ZIP JSON and, unless ``--skip-input-fetch`` is
used, checks the free Census Gazetteer + bulk ACS input coverage. It does not
call Google Places and does not change any scores.

Outputs:
  * data/processed/national_demand_qa.json
  * data/reports/national_demand_qa.html
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import census_bulk
from app.config import (
    CACHE_DB,
    CACHE_ENABLED,
    PROCESSED_DIR,
    PRODUCT_NAME,
    PRODUCT_STATUS,
    PRODUCT_VERSION,
    REPORTS_DIR,
)
from app.reports.report_export import (
    NATIONAL_DEMAND_ENRICHED_PATH,
    NATIONAL_DEMAND_PATH,
    load_national_demand_json,
)
from app.utils.cache import Cache
from scripts.build_national_demand import features_for_zip
from scripts.build_zip_centroids import (
    fetch_gazetteer_text,
    gazetteer_url,
    parse_gazetteer_records,
)

QA_JSON_PATH = PROCESSED_DIR / "national_demand_qa.json"
QA_HTML_PATH = REPORTS_DIR / "national_demand_qa.html"
SCORE_FIELDS = ("overall", "bls_demand", "cpr_demand")
ACS_DISPLAY_FIELDS = (
    "population",
    "population_density",
    "median_income",
    "healthcare_employment_share",
)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _pct(sorted_values: List[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 3)
    pos = (len(sorted_values) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac, 3)


def score_distribution(rows: Iterable[Dict[str, Any]], field: str) -> Dict[str, Any]:
    values = sorted(v for v in (_num(row.get(field)) for row in rows) if v is not None)
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p10": None,
            "p25": None,
            "p75": None,
            "p90": None,
            "above_70": 0,
            "above_55": 0,
            "above_40": 0,
            "below_40": 0,
        }
    return {
        "count": len(values),
        "min": round(values[0], 3),
        "max": round(values[-1], 3),
        "mean": round(statistics.fmean(values), 3),
        "median": _pct(values, 0.50),
        "p10": _pct(values, 0.10),
        "p25": _pct(values, 0.25),
        "p75": _pct(values, 0.75),
        "p90": _pct(values, 0.90),
        "above_70": sum(1 for v in values if v >= 70),
        "above_55": sum(1 for v in values if v >= 55),
        "above_40": sum(1 for v in values if v >= 40),
        "below_40": sum(1 for v in values if v < 40),
    }


def _compact_row(row: Dict[str, Any], field: str = "overall") -> Dict[str, Any]:
    return {
        "zip": str(row.get("zip") or ""),
        "state": row.get("state") or "",
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        field: row.get(field),
        "overall": row.get("overall"),
        "bls_demand": row.get("bls_demand"),
        "cpr_demand": row.get("cpr_demand"),
        "population": row.get("population"),
        "population_density": row.get("population_density"),
        "data_confidence": row.get("data_confidence"),
        "recommendation": row.get("recommendation"),
        "recommended_next_action": row.get("recommended_next_action"),
        "risk_flags": row.get("risk_flags") or [],
    }


def top_rows(rows: Iterable[Dict[str, Any]], field: str,
             limit: int = 50) -> List[Dict[str, Any]]:
    ranked = [row for row in rows if _num(row.get(field)) is not None]
    ranked.sort(key=lambda r: (-(_num(r.get(field)) or 0), str(r.get("zip") or "")))
    return [_compact_row(row, field) for row in ranked[:limit]]


def detect_outliers(rows: Iterable[Dict[str, Any]], limit: int = 50) -> Dict[str, Any]:
    rows = list(rows)

    def take(pred):
        return [_compact_row(row) for row in rows if pred(row)][:limit]

    return {
        "high_score_low_population": take(
            lambda r: (_num(r.get("overall")) or 0) >= 70
            and (_num(r.get("population")) or 0) < 5000
        ),
        "high_score_low_density": take(
            lambda r: (_num(r.get("overall")) or 0) >= 70
            and (_num(r.get("population_density")) or 0) < 300
        ),
        "missing_coordinates": take(
            lambda r: _num(r.get("lat")) is None or _num(r.get("lng")) is None
        ),
        "missing_acs_fields": take(
            lambda r: any(_num(r.get(field)) is None for field in ACS_DISPLAY_FIELDS)
        ),
        "high_income_weak_population_density": take(
            lambda r: (_num(r.get("median_income")) or 0) >= 150000
            and (
                (_num(r.get("population")) or 0) < 5000
                or (_num(r.get("population_density")) or 0) < 300
            )
        ),
        "high_density_weak_demand": take(
            lambda r: (_num(r.get("population_density")) or 0) >= 8000
            and (_num(r.get("overall")) or 0) < 40
        ),
    }


def state_summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        state = row.get("state")
        if state:
            groups[str(state)].append(row)
    out = {}
    for state, group in sorted(groups.items()):
        out[state] = {
            "zip_count": len(group),
            "overall": score_distribution(group, "overall"),
            "top_overall": top_rows(group, "overall", limit=10),
        }
    return out


def omission_reasons(
    matched_zips: Iterable[str],
    scored_zips: Iterable[str],
    gazetteer: Dict[str, Dict[str, float]],
    acs_by_zip: Dict[str, Dict[str, Optional[float]]],
) -> Dict[str, int]:
    reasons: Counter[str] = Counter()
    for zip_code in sorted(set(matched_zips) - set(scored_zips)):
        geo = gazetteer.get(zip_code) or {}
        acs = acs_by_zip.get(zip_code) or {}
        features = features_for_zip(geo, acs)
        if not acs:
            reasons["missing_acs_record"] += 1
        elif all(features.get(k) is None for k in features):
            reasons["no_usable_scoring_signals"] += 1
        elif features.get("population") is None:
            reasons["missing_population"] += 1
        elif features.get("population_density") is None:
            reasons["missing_land_area_or_density"] += 1
        else:
            reasons["other"] += 1
    return dict(reasons)


def build_qa_report(
    national_payload: Dict[str, Any],
    *,
    national_path: str = "",
    gazetteer: Optional[Dict[str, Dict[str, float]]] = None,
    acs_by_zip: Optional[Dict[str, Dict[str, Optional[float]]]] = None,
    input_warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    rows = list(national_payload.get("rows") or [])
    row_zips = {str(row.get("zip")) for row in rows if row.get("zip")}
    gazetteer = gazetteer or {}
    acs_by_zip = acs_by_zip or {}
    matched = set(gazetteer) & set(acs_by_zip) if gazetteer and acs_by_zip else set()
    land_zips = {
        zip_code for zip_code, geo in gazetteer.items()
        if _num((geo or {}).get("land_sqmi")) and (_num((geo or {}).get("land_sqmi")) or 0) > 0
    }
    confidence = Counter(str(row.get("data_confidence") or "missing") for row in rows)
    omitted = (len(matched - row_zips) if matched else None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": {
            "name": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "status": PRODUCT_STATUS,
        },
        "national_path": national_path,
        "input_counts": {
            "gazetteer_rows_loaded": len(gazetteer) if gazetteer else None,
            "acs_rows_loaded": len(acs_by_zip) if acs_by_zip else None,
            "matched_zip_zcta_rows": len(matched) if matched else None,
            "input_warnings": input_warnings or [],
        },
        "output_counts": {
            "total_modeled_rows": len(rows),
            "rows_with_lat_lng": sum(
                1 for row in rows
                if _num(row.get("lat")) is not None and _num(row.get("lng")) is not None
            ),
            "rows_with_population": sum(1 for row in rows if _num(row.get("population")) is not None),
            "rows_with_land_area": sum(
                1 for row in rows if str(row.get("zip")) in land_zips
            ) if land_zips else None,
            "rows_with_usable_scoring_signals": sum(
                1 for row in rows if _num(row.get("overall")) is not None
            ),
            "rows_omitted_from_scoring": omitted,
            "omission_reasons": (
                omission_reasons(matched, row_zips, gazetteer, acs_by_zip)
                if matched else {}
            ),
        },
        "data_confidence_counts": {
            "ok": confidence.get("ok", 0),
            "partial": confidence.get("partial", 0),
            "missing": confidence.get("missing", 0),
        },
        "score_distributions": {
            field: score_distribution(rows, field) for field in SCORE_FIELDS
        },
        "top_zips": {
            field: top_rows(rows, field, limit=50) for field in SCORE_FIELDS
        },
        "suspicious_outliers": detect_outliers(rows),
        "state_summary": state_summary(rows),
    }


def _load_gazetteer(args) -> Dict[str, Dict[str, float]]:
    if args.gazetteer_file:
        src = Path(args.gazetteer_file)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                text = zf.read(names[0]).decode("latin-1")
        else:
            text = src.read_text(encoding="latin-1")
    else:
        text = fetch_gazetteer_text(gazetteer_url(args.gazetteer_year))
    return parse_gazetteer_records(text)


def _render_html(report: Dict[str, Any]) -> str:
    counts = report["output_counts"]
    dist = report["score_distributions"]["overall"]
    top = report["top_zips"]["overall"][:20]
    outlier_counts = {
        key: len(value) for key, value in report["suspicious_outliers"].items()
    }
    rows = "".join(
        f"<tr><td>{row['zip']}</td><td>{row.get('overall')}</td>"
        f"<td>{row.get('population') or ''}</td>"
        f"<td>{row.get('population_density') or ''}</td>"
        f"<td>{row.get('data_confidence') or ''}</td></tr>"
        for row in top
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>National Demand QA</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:28px;line-height:1.35;color:#1d1d1f}}table{{border-collapse:collapse;width:100%;max-width:960px}}td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}}code{{background:#f4f4f6;padding:2px 4px;border-radius:4px}}</style>
</head><body>
<h1>National Demand QA</h1>
<p>Generated: <code>{report.get('generated_at')}</code></p>
<h2>Coverage</h2>
<ul>
  <li>Total modeled rows: {counts.get('total_modeled_rows')}</li>
  <li>Rows with coordinates: {counts.get('rows_with_lat_lng')}</li>
  <li>Rows with population: {counts.get('rows_with_population')}</li>
  <li>Rows with land area: {counts.get('rows_with_land_area')}</li>
  <li>Rows omitted from scoring: {counts.get('rows_omitted_from_scoring')}</li>
</ul>
<h2>Overall Score Distribution</h2>
<pre>{json.dumps(dist, indent=2)}</pre>
<h2>Outlier Counts</h2>
<pre>{json.dumps(outlier_counts, indent=2)}</pre>
<h2>Top Overall ZIPs</h2>
<table><thead><tr><th>ZIP</th><th>Overall</th><th>Population</th><th>Density</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def _default_national_path() -> Path:
    return NATIONAL_DEMAND_ENRICHED_PATH if NATIONAL_DEMAND_ENRICHED_PATH.exists() else NATIONAL_DEMAND_PATH


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--national", default=str(_default_national_path()),
                    help="Modeled demand JSON to QA.")
    ap.add_argument("--output", default=str(QA_JSON_PATH),
                    help="QA JSON output path.")
    ap.add_argument("--html-output", default=str(QA_HTML_PATH),
                    help="QA HTML output path.")
    ap.add_argument("--skip-input-fetch", action="store_true",
                    help="Do not load Gazetteer/ACS inputs; QA only the modeled JSON.")
    ap.add_argument("--gazetteer-year", type=int, default=2024)
    ap.add_argument("--gazetteer-file", default="",
                    help="Optional local Gazetteer .txt/.zip instead of downloading.")
    ap.add_argument("--acs-year", type=int, default=census_bulk.DEFAULT_ACS_YEAR)
    ap.add_argument("--no-cache", action="store_true",
                    help="Bypass the ACS cache if input fetching is enabled.")
    args = ap.parse_args(argv)

    national_path = Path(args.national)
    payload = load_national_demand_json(national_path)
    gazetteer: Dict[str, Dict[str, float]] = {}
    acs_by_zip: Dict[str, Dict[str, Optional[float]]] = {}
    warnings: List[str] = []
    if not args.skip_input_fetch:
        try:
            gazetteer = _load_gazetteer(args)
        except Exception as exc:  # pragma: no cover - network/environment guard
            warnings.append(f"gazetteer_load_failed: {exc}")
        try:
            cache = None if args.no_cache or not CACHE_ENABLED else Cache(CACHE_DB)
            acs_by_zip = census_bulk.fetch_acs_zcta_bulk(args.acs_year, cache=cache)
        except Exception as exc:  # pragma: no cover - network/environment guard
            warnings.append(f"acs_load_failed: {exc}")

    report = build_qa_report(
        payload,
        national_path=str(national_path),
        gazetteer=gazetteer,
        acs_by_zip=acs_by_zip,
        input_warnings=warnings,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    html_out = Path(args.html_output)
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(_render_html(report), encoding="utf-8")
    print(
        f"QA wrote {out} and {html_out} "
        f"({report['output_counts']['total_modeled_rows']} modeled rows)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
