"""
Auto-generate the commercial rent override skeleton from a scored report.

This automates the tedious, error-prone step of hand-copying each candidate
area's coordinates into ``data/raw/rent_overrides.csv``. It writes one row per
scored area with ``city``/``latitude``/``longitude``/``radius_miles``
pre-filled and ``rent_per_sqft_annual`` left blank for you to fill from a
cited source.

It deliberately does NOT fetch rent values. There is no free, scrape-free,
API-accessible source of commercial lease rates, and this project's stated
principle is "we do not scrape paid listing sites, and we do not invent rent
comps" (see app/collectors/real_estate.py). Plug a keyed commercial
real-estate data API into the rent collector if you want fully automated
values; this script only removes the manual coordinate bookkeeping.

Example:
    python scripts/generate_rent_template.py \
        --input data/scored/neighborhood_scored.json \
        --output data/raw/rent_overrides.csv \
        --radius-miles 2 --merge

With ``--merge`` (recommended), rows you have already filled in are preserved:
a freshly generated blank row is dropped when an existing row with a non-empty
rent sits within ``--merge-distance-miles`` of the same area.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.real_estate import OVERRIDE_FILE, RENT_OVERRIDE_COLUMNS
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger

logger = get_logger("generate_rent_template")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True,
                    help="Scored JSON report (data/scored/*.json).")
    ap.add_argument("--output", default=str(OVERRIDE_FILE),
                    help="rent_overrides.csv path to write.")
    ap.add_argument("--radius-miles", type=float, default=2.0,
                    help="Match radius written for each generated row.")
    ap.add_argument("--merge", action="store_true",
                    help="Preserve already-filled rows in an existing output "
                         "file instead of overwriting them.")
    ap.add_argument("--merge-distance-miles", type=float, default=0.3,
                    help="An existing filled row within this distance of a "
                         "generated area is treated as the same location.")
    return ap.parse_args(argv)


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _area_name(profile: Dict[str, Any]) -> str:
    return str(
        profile.get("comparison_area")
        or profile.get("candidate_name")
        or profile.get("candidate_id")
        or "unknown area"
    )


def _generated_rows(payload: Dict[str, Any], radius_miles: float
                    ) -> List[Dict[str, str]]:
    """One blank rent row per scored candidate area."""
    rows: List[Dict[str, str]] = []
    seen: set = set()
    for candidate in payload.get("candidates") or []:
        profile = candidate.get("profile") or {}
        lat = _parse_float(profile.get("latitude"))
        lon = _parse_float(profile.get("longitude"))
        if lat is None or lon is None:
            continue
        # Collapse duplicate coordinates (grid points can repeat).
        key = (round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "city": "",  # blank: match purely on coordinates + radius
            "state": str(profile.get("state") or "").upper(),
            "latitude": f"{lat:.5f}",
            "longitude": f"{lon:.5f}",
            "radius_miles": f"{radius_miles:g}",
            "rent_per_sqft_annual": "",
            "source_url": "",
            "notes": f"FILL rent_per_sqft_annual ($/sqft/year) for "
                     f"{_area_name(profile)}",
        })
    return rows


def _load_existing_filled(path: Path) -> List[Dict[str, str]]:
    """Existing rows that already have a usable rent value."""
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"could not read existing {path}: {exc}")
        return []
    filled: List[Dict[str, str]] = []
    for row in rows:
        if _parse_float(row.get("rent_per_sqft_annual")) is not None:
            filled.append({col: (row.get(col) or "") for col in RENT_OVERRIDE_COLUMNS})
    return filled


def _is_same_location(row_a: Dict[str, str], row_b: Dict[str, str],
                      max_distance_miles: float) -> bool:
    """True when two rent rows describe the same place.

    Used by --merge so regenerating the template never discards a rent value
    you already filled in. Two rows are the same location when their
    coordinates are within `max_distance_miles` of each other.
    """
    a_lat, a_lon = _parse_float(row_a.get("latitude")), _parse_float(row_a.get("longitude"))
    b_lat, b_lon = _parse_float(row_b.get("latitude")), _parse_float(row_b.get("longitude"))
    if None in (a_lat, a_lon, b_lat, b_lon):
        return False
    return haversine_miles((a_lat, a_lon), (b_lat, b_lon)) <= max_distance_miles


def build_rows(payload: Dict[str, Any], radius_miles: float,
               existing_filled: List[Dict[str, str]],
               merge_distance_miles: float) -> List[Dict[str, str]]:
    """Merge generated blank rows with already-filled rows.

    A generated row is replaced by an existing filled row when the two are the
    same location; filled rows for areas no longer scored are still kept so a
    rent value is never silently lost.
    """
    generated = _generated_rows(payload, radius_miles)
    out: List[Dict[str, str]] = []
    used_existing: List[int] = []
    for gen in generated:
        match = None
        for idx, existing in enumerate(existing_filled):
            if idx in used_existing:
                continue
            if _is_same_location(gen, existing, merge_distance_miles):
                match = existing
                used_existing.append(idx)
                break
        out.append(match if match else gen)
    # Keep filled rows that did not match any current area.
    for idx, existing in enumerate(existing_filled):
        if idx not in used_existing:
            out.append(existing)
    return out


def write_rows(rows: List[Dict[str, str]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(RENT_OVERRIDE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in RENT_OVERRIDE_COLUMNS})


def run(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    import json
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = Path(args.output)
    existing = _load_existing_filled(output) if args.merge else []
    rows = build_rows(payload, args.radius_miles, existing,
                      args.merge_distance_miles)
    write_rows(rows, output)
    filled = sum(
        1 for r in rows
        if _parse_float(r.get("rent_per_sqft_annual")) is not None
    )
    logger.info(
        f"Wrote {len(rows)} rent override row(s) to {output} "
        f"({filled} already filled, {len(rows) - filled} need a value)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
