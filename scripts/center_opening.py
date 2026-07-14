"""
Center-opening decision summary — JSON + CSV + terminal.

Maps the existing course opportunity graph to one business decision per
course (Open / Prioritize · Test first · Keep watching · Avoid for now),
with reasons, risks, and a suggested next action. No new scoring.

Example:
    python scripts/center_opening.py \
        --input data/scored/scored_locations.json \
        --enrollware-file data/raw/enrollware_classes.example.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import enrollware as _enrollware
from app.config import SCORED_DIR
from app.evaluation.center_opening import (
    build_center_opening_recommendations,
    format_terminal_summary,
    write_recommendations_csv,
    write_recommendations_json,
)
from app.reports.interpretation import (
    aggregate_demand_counts,
    build_course_performance_section,
)

PROCESSED_DIR = ROOT / "data" / "processed"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(SCORED_DIR / "scored_locations.json"),
                    help="Scored JSON report (for city/candidate context).")
    ap.add_argument("--enrollware-file", default="",
                    help="Enrollware export (.xlsx/.csv). Used when the scored "
                         "JSON does not already carry course_performance.")
    ap.add_argument("--json-output",
                    default=str(PROCESSED_DIR / "center_opening_recommendations.json"))
    ap.add_argument("--csv-output",
                    default=str(PROCESSED_DIR / "center_opening_recommendations.csv"))
    return ap.parse_args()


def run() -> int:
    args = parse_args()
    payload = {}
    input_path = Path(args.input)
    if input_path.exists():
        payload = json.loads(input_path.read_text(encoding="utf-8"))

    candidates = payload.get("candidates") or []
    perf = (payload.get("context") or {}).get("course_performance")

    if not perf:
        records = _enrollware.load_records(
            Path(args.enrollware_file) if args.enrollware_file else None)
        if records:
            ranked = [(c.get("profile") or {}, c.get("scored") or {})
                      for c in candidates]
            cities = {str((c.get("profile") or {}).get("city") or "")
                      for c in candidates}
            states = {str((c.get("profile") or {}).get("state") or "")
                      for c in candidates}
            city = (next(iter(cities - {""}), None)
                    if len(cities - {""}) == 1 else None)
            state = (next(iter(states - {""}), None)
                     if len(states - {""}) == 1 else None)
            perf = build_course_performance_section(
                records, city=city, state=state,
                demand_counts=aggregate_demand_counts(ranked) if ranked else None,
            )

    # Location context: the top-ranked candidate's name, when there is one.
    # Carried through as-is, never guessed.
    location = None
    if candidates:
        top = (candidates[0].get("profile") or {})
        location = top.get("name") or top.get("formatted_address")

    recs = build_center_opening_recommendations(perf, location=location)
    write_recommendations_json(recs, Path(args.json_output))
    write_recommendations_csv(recs, Path(args.csv_output))
    print(format_terminal_summary(recs))
    print(f"\nSaved -> {args.json_output}")
    print(f"Saved -> {args.csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
