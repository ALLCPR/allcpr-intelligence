"""
Select ZIPs worth live Google Places enrichment using offline signals only.

This script reads the modeled national layer, applies hard exclusions and a
cheap API-candidate score, then writes a budget plan. It never calls Google
Places or any other live paid API.

The selected ZIPs are finalists for context/validation enrichment. They are not
evidence that Places should become the national scoring engine; that remains
backtest-gated after the 0.103 → 0.105 flat Places scoring result.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import PROCESSED_DIR
from app.reports.report_export import NATIONAL_DEMAND_ENRICHED_PATH, NATIONAL_DEMAND_PATH
from app.scoring.api_candidate_filter import (
    annotate_api_candidate,
    estimated_places_calls,
    estimated_runtime_minutes,
    filter_api_candidates,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

API_CANDIDATES_PATH = PROCESSED_DIR / "api_candidates.json"
EARTH_RADIUS_MILES = 3958.7613


def _default_input_path() -> Path:
    return (NATIONAL_DEMAND_ENRICHED_PATH
            if NATIONAL_DEMAND_ENRICHED_PATH.exists()
            else NATIONAL_DEMAND_PATH)


def haversine_miles(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    """Return great-circle distance in miles between two lat/lng points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lng2 - lng1)
    a = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def filter_rows_by_radius(
    rows: List[Dict[str, Any]],
    *,
    center_lat: float | None = None,
    center_lng: float | None = None,
    radius_miles: float | None = None,
) -> List[Dict[str, Any]]:
    """Filter rows to a local radius using already-modeled coordinates only."""
    if center_lat is None and center_lng is None and radius_miles is None:
        return list(rows)
    if center_lat is None or center_lng is None or radius_miles is None:
        raise ValueError(
            "--center-lat, --center-lng, and --radius-miles must be supplied together."
        )
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            lat = float(row.get("lat"))
            lng = float(row.get("lng"))
        except (TypeError, ValueError):
            continue
        distance = haversine_miles(center_lat, center_lng, lat, lng)
        if distance <= radius_miles:
            annotated = dict(row)
            annotated["api_filter_distance_miles"] = round(distance, 2)
            out.append(annotated)
    return out


def build_api_candidate_payload(
    rows: List[Dict[str, Any]],
    *,
    top: int | None = None,
    min_score: float | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
    radius_miles: float | None = None,
) -> Dict[str, Any]:
    """Build the API budget payload from already-loaded ZIP rows."""
    scoped_rows = filter_rows_by_radius(
        rows,
        center_lat=center_lat,
        center_lng=center_lng,
        radius_miles=radius_miles,
    )
    annotated = [annotate_api_candidate(row) for row in scoped_rows]
    excluded = [r for r in annotated if r["api_priority"] == "exclude"]
    candidates = [r for r in annotated if r["api_priority"] != "exclude"]
    selected = filter_api_candidates(
        scoped_rows, max_zips=top, min_score=min_score)
    compact_rows = [
        {
            "zip": row.get("zip"),
            "api_candidate_score": row.get("api_candidate_score"),
            "api_priority": row.get("api_priority"),
            "recommended_for_live_places": row.get("recommended_for_live_places"),
            "distance_miles": row.get("api_filter_distance_miles"),
            "reason": row.get("api_filter_reason"),
        }
        for row in selected
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_zips": len(rows),
        "scoped_zips": len(scoped_rows),
        "excluded_zips": len(excluded),
        "candidate_zips": len(candidates),
        "selected_zips": len(selected),
        "estimated_places_calls": estimated_places_calls(len(selected)),
        "estimated_runtime_minutes": estimated_runtime_minutes(len(selected)),
        "places_calls_per_zip": 4,
        "selection": {
            "top": top,
            "min_score": min_score,
            "center_lat": center_lat,
            "center_lng": center_lng,
            "radius_miles": radius_miles,
        },
        "rows": compact_rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(_default_input_path()),
                    help="National demand JSON; enriched is preferred by default.")
    ap.add_argument("--output", default=str(API_CANDIDATES_PATH))
    ap.add_argument("--top", type=int, default=None,
                    help="Select at most this many ranked ZIPs.")
    ap.add_argument("--min-score", type=float, default=None,
                    help="Select only ZIPs at or above this API-candidate score.")
    ap.add_argument("--center-lat", type=float, default=None,
                    help="Latitude for offline radius filtering.")
    ap.add_argument("--center-lng", type=float, default=None,
                    help="Longitude for offline radius filtering.")
    ap.add_argument("--radius-miles", type=float, default=None,
                    help="Radius in miles around --center-lat/--center-lng.")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        logger.error(f"Input not found: {in_path}. Run build_national_demand.py first.")
        return 1
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    try:
        out_payload = build_api_candidate_payload(
            rows,
            top=args.top,
            min_score=args.min_score,
            center_lat=args.center_lat,
            center_lng=args.center_lng,
            radius_miles=args.radius_miles,
        )
    except ValueError as exc:
        logger.error(str(exc))
        return 2
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_payload), encoding="utf-8")
    logger.info(
        f"API candidates: {out_payload['selected_zips']} selected / "
        f"{out_payload['candidate_zips']} candidates / "
        f"{out_payload['scoped_zips']} scoped / "
        f"{out_payload['excluded_zips']} excluded. "
        f"Estimated Places calls: {out_payload['estimated_places_calls']} "
        f"(~{out_payload['estimated_runtime_minutes']} min) → {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
