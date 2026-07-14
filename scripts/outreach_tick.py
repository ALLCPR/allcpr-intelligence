#!/usr/bin/env python3
"""
Outreach engine heartbeat — poll replies + send due follow-up emails.

Hits the hosted instance's authenticated tick endpoint. Point a cron at this
(e.g. once or twice a day on weekdays); staff can also click "Check Replies +
Send Follow-ups" in the dashboard, which does the same thing.

    # crontab: weekdays at 9:30 and 14:30
    30 9,14 * * 1-5  python3 scripts/outreach_tick.py \
        --url https://allcpr-site-intelligence.onrender.com

The tick is safe to run any time: replies only ever stop sequences, follow-ups
only send when due and under the daily cap, and in DRY_RUN mode (no SMTP creds
on the server) nothing sends at all.
"""
from __future__ import annotations

import argparse
import os

import requests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--url", required=True,
                    help="Base URL of the hosted dashboard")
    ap.add_argument("--user", default=os.environ.get("DASHBOARD_USER", "allcpr"))
    ap.add_argument("--password",
                    default=os.environ.get("DASHBOARD_PASSWORD", ""))
    ap.add_argument("--write-token",
                    default=os.environ.get("OPS_WRITE_TOKEN", ""),
                    help="X-Ops-Write-Token if the server sets OPS_WRITE_TOKEN")
    args = ap.parse_args()
    url = args.url.rstrip("/") + "/api/ops/outreach/tick"
    auth = (args.user, args.password) if args.password else None
    headers = {"X-Ops-Write-Token": args.write_token} if args.write_token else {}
    resp = requests.post(url, auth=auth, headers=headers, timeout=120)
    if resp.status_code != 200:
        print(f"error: HTTP {resp.status_code}: {resp.text[:500]}")
        return 1
    body = resp.json()
    print(f"mode={body.get('mode')} replies={len(body.get('replied') or [])} "
          f"opt_out={len(body.get('opted_out') or [])} "
          f"follow_ups={len(body.get('followed_up') or [])} "
          f"dry_run={len(body.get('dry_run') or [])} "
          f"capped={len(body.get('capped') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
