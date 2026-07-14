#!/usr/bin/env python3
"""
Daily expansion-ops scan — build the action queue and (optionally) sync Manatal.

Hits the hosted instance's read endpoints so the whole loop runs on a schedule
instead of only on a click:
  * GET /api/ops/action-queue  → recompute instructor matching, coverage, and
    the grouped daily task list (the boss/helper checklist).
  * --sync-manatal → for each ZIP flagged "Manatal Sync Needed", pull the
    latest Manatal stage back into readiness (no-op if Manatal is disabled).

Point a Render Cron Job (or crontab) at this once a day. Read-only by default;
--sync-manatal only reads Manatal + updates the local store.

    python scripts/daily_scan.py --url https://allcpr-site-intelligence.onrender.com
    python scripts/daily_scan.py --url ... --zips 94541,95112 --sync-manatal
"""
from __future__ import annotations

import argparse
import os

import requests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--url", required=True, help="Base URL of the dashboard")
    ap.add_argument("--zips", default="",
                    help="Comma-separated ZIPs (default: ZIPs with activity)")
    ap.add_argument("--sync-manatal", action="store_true",
                    help="Pull Manatal stages for ZIPs that need a sync")
    ap.add_argument("--write-token",
                    default=os.environ.get("OPS_WRITE_TOKEN", ""))
    args = ap.parse_args()
    base = args.url.rstrip("/")
    headers = {"X-Ops-Write-Token": args.write_token} if args.write_token else {}

    q = requests.get(f"{base}/api/ops/action-queue",
                     params={"zips": args.zips}, timeout=120)
    if q.status_code != 200:
        print(f"error: action-queue HTTP {q.status_code}: {q.text[:300]}")
        return 1
    data = q.json()
    print(f"Action queue for {len(data.get('generated_for_zips') or [])} ZIP(s) "
          f"— {data.get('total_tasks', 0)} task(s), Manatal {data.get('manatal_mode')}")
    for group, tasks in (data.get("groups") or {}).items():
        if tasks:
            print(f"  {group}: {len(tasks)}")
            for t in tasks[:5]:
                print(f"    [{t['priority']}] {t['ref']} — {t['next_action']}")

    if args.sync_manatal:
        need = data.get("groups", {}).get("Manatal Sync Needed", [])
        for task in need:
            zip_code = task.get("ref")
            r = requests.get(f"{base}/api/ops/manatal/zip/{zip_code}/sync-status",
                             headers=headers, timeout=120)
            print(f"  manatal sync {zip_code}: HTTP {r.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
