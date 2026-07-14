"""
Back-test the scoring model against real ALLCPR site outcomes.

The model's weights and caps are guesses until they're graded against
reality. Supply a CSV of locations you already know the outcome for —
existing ALLCPR centers, or any site whose performance you can score — and
this reports how well ``site_score`` (and each sub-score) actually predicts
that outcome.

Input CSV columns (header required):
    address        free-text address or "City, ST"
    state          two-letter state (used if not embedded in address)
    outcome        numeric outcome — monthly enrollment, revenue, a 0-100
                   success rating, 1/0 survived, etc. Higher = better.
    label          optional human label for the row

Example:
    address,state,outcome,label
    "1631 N First St San Jose 95112",CA,420,"San Jose center (strong)"
    "500 Market St San Francisco",CA,180,"SF pilot (weak)"
    ...

Usage:
    python scripts/backtest.py --input sites_with_outcomes.csv \
        --outcome-name "monthly_enrollment" --radius-miles 2

Notes:
- Each row runs the same enrichment + scoring the live pipeline uses, so
  this is a faithful grade of the model as shipped.
- Uses the response cache, so re-running a back-test is cheap.
- Needs at least 3 rows for a correlation; 8+ for anything trustworthy.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import bls_or_labor as _bls_or_labor
from app.collectors import census as _census
from app.collectors import job_postings as _job_postings
from app.collectors.google_places import GooglePlacesClient
from app.config import CACHE_DB, CACHE_ENABLED
from app.enrichers.area_profile import build_area_profile
from app.scoring.backtest import analyze_backtest, format_report
from app.scoring.site_score import score_profile
from app.utils.cache import Cache
from app.utils.geo_utils import geocode_city
from app.utils.logging_utils import get_logger

logger = get_logger("backtest")


def _load_rows(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"address", "outcome"}
    if not rows or not required <= set(rows[0].keys()):
        raise SystemExit(
            f"Input CSV must have at least columns {sorted(required)}; "
            f"found {sorted(rows[0].keys()) if rows else 'no rows'}"
        )
    return rows


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True,
                    help="CSV of locations with known outcomes.")
    ap.add_argument("--state", default="", help="Default state code.")
    ap.add_argument("--outcome-name", default="outcome",
                    help="Label for the outcome metric in the report.")
    ap.add_argument("--radius-miles", type=float, default=2.0,
                    help="Catchment radius used when scoring each site.")
    return ap.parse_args(argv)


def run(argv=None) -> int:
    args = parse_args(argv)
    rows = _load_rows(Path(args.input))

    cache_mode = "no-cache" if not CACHE_ENABLED else "auto"
    cache = Cache(CACHE_DB, mode=cache_mode)
    _census.set_cache(cache)
    _bls_or_labor.set_cache(cache)
    _job_postings.set_cache(cache)
    client = GooglePlacesClient(cache=cache)

    scored_rows: List[Dict[str, object]] = []
    for idx, row in enumerate(rows):
        address = (row.get("address") or "").strip()
        outcome_raw = (row.get("outcome") or "").strip()
        state = (row.get("state") or args.state or "").strip()
        label = (row.get("label") or address).strip()
        if not address or not outcome_raw:
            logger.warning(f"skipping row {idx}: missing address/outcome")
            continue
        try:
            outcome = float(outcome_raw)
        except ValueError:
            logger.warning(f"skipping {label!r}: non-numeric outcome {outcome_raw!r}")
            continue

        center = geocode_city(address, state)
        if center is None:
            logger.warning(f"could not geocode {address!r}; skipping")
            continue

        try:
            profile = build_area_profile(
                client, address, state, center.lat, center.lon,
                args.radius_miles, candidate_index=idx,
                candidate_name=label, candidate_source="backtest",
            )
            scored = score_profile(profile)
        except Exception as exc:
            logger.warning(f"scoring failed for {label!r}: {exc}")
            continue

        logger.info(f"{label}: site_score={scored.get('site_score')} "
                    f"outcome={outcome}")
        scored_rows.append({
            "outcome": outcome,
            "site_score": scored.get("site_score"),
            "sub_scores": scored.get("sub_scores") or {},
            "label": label,
        })

    if not scored_rows:
        raise SystemExit("No scorable rows — check addresses and outcomes.")

    report = analyze_backtest(scored_rows, outcome_name=args.outcome_name)
    print()
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
