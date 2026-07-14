#!/usr/bin/env python3
"""Generate boss-readable center-opening recommendation JSON/CSV.

Default command:

    python scripts/generate_center_recommendations.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.center_recommendations import (  # noqa: E402
    build_center_recommendations_from_report,
    format_terminal_summary,
    write_recommendations_csv,
    write_recommendations_json,
)

PROCESSED_DIR = ROOT / "data" / "processed"
DEFAULT_INPUTS = (
    ROOT / "data" / "scored" / "sj_scored.json",
    ROOT / "data" / "scored" / "scored_locations.json",
)


def _default_input() -> str:
    for path in DEFAULT_INPUTS:
        if path.exists():
            return str(path)
    return str(DEFAULT_INPUTS[-1])


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=_default_input(),
                    help="Scored JSON report to summarize.")
    ap.add_argument("--json-output",
                    default=str(PROCESSED_DIR / "center_opening_recommendations.json"))
    ap.add_argument("--csv-output",
                    default=str(PROCESSED_DIR / "center_opening_recommendations.csv"))
    ap.add_argument("--top-n", type=int, default=0,
                    help="Limit recommendations to the top N candidates; 0 means all.")
    return ap.parse_args()


def run() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Input JSON not found: {input_path}")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    recs = build_center_recommendations_from_report(
        payload, limit=args.top_n or None,
    )
    write_recommendations_json(recs, Path(args.json_output))
    write_recommendations_csv(recs, Path(args.csv_output))
    print(format_terminal_summary(recs))
    print(f"\nSaved -> {args.json_output}")
    print(f"Saved -> {args.csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
