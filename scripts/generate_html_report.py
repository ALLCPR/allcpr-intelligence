"""
Generate a print-friendly HTML report from scored_locations.json.

Example:
    python scripts/generate_html_report.py \
        --input data/scored/scored_locations.json \
        --output data/reports/allcpr_site_report.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import enrollware as _enrollware
from app.config import REPORTS_DIR, SCORED_DIR
from app.enrichers.historical_performance import build_candidate_historical_performance
from app.scoring import zip_demand as _zip_demand
from app.reports import ai_summary as _ai_summary
from app.reports.html_report import load_json_report, write_html_report
from app.reports.easy_html_report import easy_output_path, write_easy_html_report
from app.reports.report_export import LATEST_REPORT_PATH, write_latest_report_json
from app.reports.interpretation import (
    aggregate_demand_counts,
    build_course_performance_section,
)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(SCORED_DIR / "scored_locations.json"),
                    help="Input scored JSON report.")
    ap.add_argument("--output", default=str(REPORTS_DIR / "allcpr_site_report.html"),
                    help="Output HTML path.")
    ap.add_argument("--top-n", type=int, default=None,
                    help="Optional cap on rendered candidate cards.")
    ap.add_argument("--title", default="ALLCPR Site Intelligence Report",
                    help="HTML report title.")
    ap.add_argument("--report-style",
                    choices=("executive", "detailed", "debug"),
                    default=None,
                    help="Presentation style. Defaults to the style stored in "
                         "the JSON report context, or 'executive'.")
    ap.add_argument("--enrollware-file", default="",
                    help="Optional Enrollware export (.xlsx / .csv). When given "
                         "(or when data/raw/Enrollware Data - Classes.xlsx or "
                         "data/raw/enrollware_classes.* exists), the "
                         "Phase 4B course-performance sections are computed and "
                         "injected before rendering — no API pipeline re-run "
                         "needed.")
    ap.add_argument("--enrollware-locations-file", default="",
                    help="Optional Enrollware Locations export (.xlsx / .csv) "
                         "used to resolve class-location abbreviations into "
                         "city/state before rendering.")
    ap.add_argument("--no-enrollware", action="store_true",
                    help="Do not attach course-performance sections.")
    ap.add_argument("--ai-summary", action="store_true",
                    help="Generate an AI executive summary (OpenAI/Groq) from "
                         "the report data and inject it before rendering. "
                         "Requires GROQ_API_KEY or OPENAI_API_KEY.")
    ap.add_argument("--easy-report", dest="easy_report", action="store_true",
                    default=True,
                    help="Also write a clean boss-facing easy report "
                         "(default: on).")
    ap.add_argument("--no-easy-report", dest="easy_report", action="store_false",
                    help="Do not write the easy executive report.")
    ap.add_argument("--dashboard-json", default=str(LATEST_REPORT_PATH),
                    help="Output path for the web-dashboard JSON payload "
                         "consumed by web_app.py.")
    return ap.parse_args()


def _attach_course_performance(
    payload: dict,
    enrollware_file: str,
    enrollware_locations_file: str = "",
) -> None:
    """Compute + inject course performance into the loaded report payload."""
    records, data_quality = _enrollware.load_enrollware(
        Path(enrollware_file) if enrollware_file else None,
        locations_path=(
            Path(enrollware_locations_file) if enrollware_locations_file else None
        ),
    )
    if not records:
        return
    demand_by_zip = _zip_demand.aggregate_zip_demand(records)
    centroids = _zip_demand.load_zip_centroids()
    reference_avg = _zip_demand.overall_reference_avg(records)
    export_latest = _zip_demand.latest_export_date(records)
    payload.setdefault("context", {})["enrollware_data_quality"] = (
        data_quality.to_dict()
    )
    candidates = payload.get("candidates") or []
    ranked = [(c.get("profile") or {}, c.get("scored") or {}) for c in candidates]
    for candidate in candidates:
        profile = candidate.get("profile") or {}
        # Always rebuild from the freshly-loaded records so the card reflects the
        # current held-class cleaning (a previously-scored payload may carry an
        # older, pre-cleaning historical_performance with future/zero rows).
        profile["historical_performance"] = build_candidate_historical_performance(
            records,
            city=profile.get("comparison_area") or profile.get("city"),
            state=profile.get("state"),
        )
        anchor = profile.get("anchor") or {}
        profile["zip_demand"] = _zip_demand.build_candidate_zip_demand(
            demand_by_zip,
            candidate_zip=_zip_demand.parse_zip(anchor.get("formatted_address")),
            latitude=profile.get("latitude"),
            longitude=profile.get("longitude"),
            city=profile.get("comparison_area") or profile.get("city"),
            centroids=centroids,
            reference_avg=reference_avg,
            latest_export_date=export_latest,
        )
    # Derive the area from the candidates (single-city reports share one city).
    cities = {
        str((c.get("profile") or {}).get("city") or "") for c in candidates
    }
    states = {
        str((c.get("profile") or {}).get("state") or "") for c in candidates
    }
    city = next(iter(cities - {""}), None) if len(cities - {""}) == 1 else None
    state = next(iter(states - {""}), None) if len(states - {""}) == 1 else None
    course_perf = build_course_performance_section(
        records, city=city, state=state,
        demand_counts=aggregate_demand_counts(ranked),
    )
    if course_perf:
        payload.setdefault("context", {})["course_performance"] = course_perf


def run() -> int:
    args = parse_args()
    payload = load_json_report(Path(args.input))
    if not args.no_enrollware:
        _attach_course_performance(
            payload, args.enrollware_file, args.enrollware_locations_file
        )
    if args.ai_summary:
        summary = _ai_summary.generate_executive_summary(payload)
        if summary:
            payload.setdefault("context", {})["ai_summary"] = summary
    main_output = Path(args.output)
    write_html_report(payload, main_output, top_n=args.top_n,
                      title=args.title, report_style=args.report_style)
    if args.easy_report:
        easy_output = easy_output_path(main_output)
        write_easy_html_report(
            payload, easy_output,
            title=f"{args.title} — Easy Executive Report",
            full_report_name=main_output.name,
            report_style=args.report_style,
        )
    # Also export the machine-readable payload consumed by the web dashboard
    # (web_app.py). Same report context — no second scoring path.
    json_path = write_latest_report_json(payload, output_path=args.dashboard_json)
    print(f"Wrote dashboard JSON → {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
