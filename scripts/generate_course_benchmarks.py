#!/usr/bin/env python3
"""Generate historical enrollment benchmarks from Enrollware history."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.enrollware import load_records  # noqa: E402
from app.evaluation.course_enrollment_benchmarks import (  # noqa: E402
    build_course_enrollment_benchmarks,
    write_benchmarks_csv,
    write_benchmarks_json,
)
from app.evaluation.course_enrollment_trends import (  # noqa: E402
    build_course_enrollment_trends,
    write_trends_csv,
    write_trends_json,
)

PROCESSED_DIR = ROOT / "data" / "processed"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--enrollware-file", default="",
                    help="Enrollware Classes export (.xlsx/.csv). Defaults to auto-discovery.")
    ap.add_argument("--enrollware-locations-file", default="",
                    help="Optional Enrollware Locations export for city metadata.")
    ap.add_argument("--json-output",
                    default=str(PROCESSED_DIR / "course_enrollment_benchmarks.json"))
    ap.add_argument("--csv-output",
                    default=str(PROCESSED_DIR / "course_enrollment_benchmarks.csv"))
    ap.add_argument("--trends-json-output",
                    default=str(PROCESSED_DIR / "course_enrollment_trends.json"))
    ap.add_argument("--trends-csv-output",
                    default=str(PROCESSED_DIR / "course_enrollment_trends.csv"))
    return ap.parse_args()


def run() -> int:
    args = parse_args()
    records = load_records(
        Path(args.enrollware_file) if args.enrollware_file else None,
        Path(args.enrollware_locations_file) if args.enrollware_locations_file else None,
    )
    payload = build_course_enrollment_benchmarks(records)
    write_benchmarks_json(payload, Path(args.json_output))
    write_benchmarks_csv(payload, Path(args.csv_output))

    trends = build_course_enrollment_trends(records)
    write_trends_json(trends, Path(args.trends_json_output))
    write_trends_csv(trends, Path(args.trends_csv_output))

    print(f"ALLCPR overall average: {payload.get('allcpr_overall_average')}")
    for row in payload.get("course_benchmarks") or []:
        print(
            f"{row['course_type']}: avg={row['average_students_per_class']} "
            f"classes={row['class_count']} vs_allcpr={row['comparison_vs_allcpr_average']}"
        )
    print(f"Strongest historical course: {payload.get('strongest_historical_course_type')}")
    print("\nHistorical enrollment trends:")
    for t in trends.get("trends") or []:
        print(
            f"{t['course_type']}: {t['trend_direction']} "
            f"(slope={t['slope']}, R²={t['r_squared']}, n={t['n']}, "
            f"{t['confidence_label']})"
        )
    print(f"\nSaved -> {args.json_output}")
    print(f"Saved -> {args.csv_output}")
    print(f"Saved -> {args.trends_json_output}")
    print(f"Saved -> {args.trends_csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
