"""
API health-check CLI.

Run before a big pipeline run (or on a schedule) to confirm every configured
data source is reachable and authenticating. Catches silent breakage —
deprecated endpoints, rotated keys, changed request formats, blown quotas —
before they quietly zero out a data source in a report you're trusting.

Usage:
    python scripts/healthcheck.py
    python scripts/healthcheck.py --strict   # exit 1 if any API is down

Exit codes:
    0  all configured APIs OK (or only skipped ones)
    1  at least one configured API is down  (only with --strict)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.utils.healthcheck import any_down, format_report, run_all


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="Exit 1 if any configured API is down.")
    args = ap.parse_args(argv)

    results = run_all()
    print(format_report(results))

    if args.strict and any_down(results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
