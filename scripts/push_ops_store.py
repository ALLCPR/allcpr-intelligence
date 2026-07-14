#!/usr/bin/env python3
"""
Push the local (gitignored) ops store to a hosted instance.

The real instructor/space leads, CRM state, and outreach log live only in
data/ops/*.json on the machine where they were imported — they are never
committed. This script uploads that snapshot to the hosted dashboard's
/api/ops/admin/import-store endpoint so the live site can show the same data.
The tool is served open, so no credentials are needed; the instance should keep
OPS_DATA_DIR on a persistent disk so the upload survives deploys. (If you later
put the deployment behind Basic auth, pass --user/--password or set
DASHBOARD_USER/DASHBOARD_PASSWORD and they'll be sent.)

Usage:
    python scripts/push_ops_store.py --url https://allcpr-site-intelligence.onrender.com

    --mode merge    (default) preserve CRM edits staff made on the live site
    --mode replace  overwrite the live store with the local snapshot
    --dry-run       show what would be uploaded without sending anything
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ops.store import (  # noqa: E402
    INSTRUCTOR_FILE,
    OPS_DATA_DIR,
    OUTREACH_LOG_FILE,
    REFRESH_STATE_FILE,
    SPACE_FILE,
)

_PAYLOAD_KEYS = {
    "instructor_candidates": INSTRUCTOR_FILE,
    "space_candidates": SPACE_FILE,
    "outreach_log": OUTREACH_LOG_FILE,
    "refresh_state": REFRESH_STATE_FILE,
}


def build_payload(ops_dir: Path) -> dict:
    payload = {}
    for key, filename in _PAYLOAD_KEYS.items():
        path = ops_dir / filename
        if not path.exists():
            continue
        try:
            payload[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {filename}: {exc}")
    return payload


def summarize(payload: dict) -> None:
    inst = payload.get("instructor_candidates") or {}
    space = payload.get("space_candidates") or {}
    print(f"  instructor candidates: "
          f"{sum(len(v) for v in inst.values())} across {len(inst)} ZIPs")
    print(f"  space candidates:      "
          f"{sum(len(v) for v in space.values())} across {len(space)} ZIPs")
    print(f"  outreach log entries:  {len(payload.get('outreach_log') or [])}")
    print(f"  refresh timestamps:    {len(payload.get('refresh_state') or {})}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--url", required=True,
                    help="Base URL of the hosted dashboard")
    ap.add_argument("--user", default=os.environ.get("DASHBOARD_USER", "allcpr"))
    ap.add_argument("--password",
                    default=os.environ.get("DASHBOARD_PASSWORD", ""))
    ap.add_argument("--mode", choices=("merge", "replace"), default="merge")
    ap.add_argument("--ops-dir", type=Path, default=OPS_DATA_DIR,
                    help="Local ops store directory (default: data/ops)")
    ap.add_argument("--write-token",
                    default=os.environ.get("OPS_WRITE_TOKEN", ""),
                    help="X-Ops-Write-Token if the server sets OPS_WRITE_TOKEN")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = build_payload(args.ops_dir)
    if not payload:
        print(f"error: nothing to upload — no store files in {args.ops_dir}")
        return 2

    print(f"Local snapshot from {args.ops_dir}:")
    summarize(payload)

    if args.dry_run:
        print("Dry run — nothing sent.")
        return 0

    url = args.url.rstrip("/") + f"/api/ops/admin/import-store?mode={args.mode}"
    print(f"Uploading ({args.mode}) to {url} ...")
    auth = (args.user, args.password) if args.password else None
    headers = {"X-Ops-Write-Token": args.write_token} if args.write_token else {}
    resp = requests.post(url, json=payload, auth=auth, headers=headers,
                         timeout=120)
    if resp.status_code != 200:
        print(f"error: HTTP {resp.status_code}: {resp.text[:500]}")
        return 1
    body = resp.json()
    print(f"Imported on server: {body.get('imported')}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
