"""
Re-score a folder of previously-enriched profile JSONs (produced by
full_pipeline --save-profiles) without hitting any external API.

Useful when iterating on scoring weights / thresholds.

Example:
    python scripts/score_locations.py \
        --input data/enriched \
        --output data/scored/scored_locations.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ENRICHED_DIR, SCORED_DIR
from app.reports.csv_report import candidate_to_row, write_csv_report
from app.scoring.site_score import score_profile
from app.utils.logging_utils import get_logger

logger = get_logger("score_locations")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(ENRICHED_DIR),
                    help="Directory of profile JSON files (one per candidate).")
    ap.add_argument("--output", default=str(SCORED_DIR / "scored_locations.csv"),
                    help="Output CSV path.")
    return ap.parse_args()


def run() -> int:
    args = parse_args()
    in_dir = Path(args.input)
    if not in_dir.exists():
        logger.error(f"Input dir not found: {in_dir}")
        return 1

    rows = []
    for jf in sorted(in_dir.glob("*.json")):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(f"Skipping malformed {jf}: {exc}")
            continue
        profile = payload.get("profile") or payload
        scored = score_profile(profile)
        rows.append(candidate_to_row(profile, scored))

    write_csv_report(rows, Path(args.output))
    logger.info(f"scored {len(rows)} profiles -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
