#!/usr/bin/env python3
"""
Push the local (gitignored) source CSVs to a hosted instance's persistent disk.

The matching / coverage / revenue-health engines read real data from
data/manual/*.csv (roster, past-instructor performance, locations, revenue
health, competitor pricing, demand). Those files are gitignored — they hold real
PII — so a hosted checkout starts empty and the features look blank on Render.
This uploads them to the instance's MANUAL_DATA_DIR (a persistent-disk path) via
the whitelisted /api/ops/admin/import-manual endpoint.

⚠️ This sends real instructor names / emails / pay data to the hosted URL. If the
site is served open, that data becomes publicly queryable — set OPS_WRITE_TOKEN
on the server (and pass it here) and consider a network allowlist first.

    OPS_WRITE_TOKEN=... python scripts/push_manual_data.py \
        --url https://allcpr-site-intelligence.onrender.com
    # --dry-run to see what would upload; --only file1.csv,file2.csv to limit
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import MANUAL_DIR                         # noqa: E402
from app.ops.imports import MANUAL_CSV_WHITELIST          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--url", required=True, help="Base URL of the dashboard")
    ap.add_argument("--manual-dir", type=Path, default=MANUAL_DIR)
    ap.add_argument("--only", default="",
                    help="Comma-separated filenames to upload (default: all found)")
    ap.add_argument("--write-token",
                    default=os.environ.get("OPS_WRITE_TOKEN", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    only = {n.strip() for n in args.only.split(",") if n.strip()}
    files = {}
    for name in sorted(MANUAL_CSV_WHITELIST):
        if only and name not in only:
            continue
        path = args.manual_dir / name
        if path.exists():
            files[name] = path.read_text(encoding="utf-8")

    if not files:
        print(f"error: no whitelisted CSVs found in {args.manual_dir}")
        return 2
    print(f"Found {len(files)} CSV(s) in {args.manual_dir}:")
    for name, content in files.items():
        print(f"  {name}: {content.count(chr(10))} rows, {len(content)} bytes")
    if args.dry_run:
        print("Dry run — nothing sent.")
        return 0

    headers = {"X-Ops-Write-Token": args.write_token} if args.write_token else {}
    url = args.url.rstrip("/") + "/api/ops/admin/import-manual"
    resp = requests.post(url, json={"files": files}, headers=headers, timeout=120)
    if resp.status_code != 200:
        print(f"error: HTTP {resp.status_code}: {resp.text[:400]}")
        return 1
    body = resp.json()
    print(f"Uploaded: {body.get('written')}")
    if body.get("skipped"):
        print(f"Skipped: {body.get('skipped')}")
    print("Done. The live engines now read this data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
