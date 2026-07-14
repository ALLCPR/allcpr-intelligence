"""
Commercial real-estate / rent signal collector.

There is no free, comprehensive, API-accessible source of commercial lease
rates in the US. The supported Phase 2 path is a cited manual CSV override:

    data/raw/rent_overrides.csv

Unknown rent stays unknown. We do not scrape paid listing sites, and we do not
invent rent comps.
"""
from __future__ import annotations

import csv
from typing import Dict, List, Optional

from app.config import RAW_DIR
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger
from app.utils.source_tracker import utcnow_iso

logger = get_logger(__name__)

STUB_FIELDS = (
    "rent_per_sqft_month",
    "rent_per_sqft_annual",
    "vacancy_rate_pct",
    "median_commercial_lease_term_months",
    "rent_data_confidence",
    "rent_source",
    "rent_notes",
)

OVERRIDE_FILE = RAW_DIR / "rent_overrides.csv"
# Canonical full column list (written by scripts/generate_rent_template.py).
# The last two columns are optional — older files that only have the required
# subset still load correctly.
RENT_OVERRIDE_COLUMNS = (
    "city",
    "state",
    "latitude",
    "longitude",
    "radius_miles",
    "rent_per_sqft_annual",
    "source_url",
    "notes",
    "vacancy_rate_pct",
    "median_commercial_lease_term_months",
)
_REQUIRED_RENT_COLUMNS = (
    "city",
    "state",
    "latitude",
    "longitude",
    "radius_miles",
    "rent_per_sqft_annual",
    "source_url",
    "notes",
)


def _empty_values(notes: str = "") -> Dict[str, object]:
    return {
        "rent_per_sqft_month": None,
        "rent_per_sqft_annual": None,
        "vacancy_rate_pct": None,
        "median_commercial_lease_term_months": None,
        "rent_data_confidence": "unknown",
        "rent_source": "",
        "rent_notes": notes or "No rent override matched; rent is unknown.",
    }


def _parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _load_overrides() -> List[Dict[str, str]]:
    if not OVERRIDE_FILE.exists():
        return []
    try:
        with open(OVERRIDE_FILE, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        logger.warning(f"real_estate: could not load {OVERRIDE_FILE}: {exc}")
        return []
    if not rows:
        return []
    columns = set(rows[0].keys())
    missing = set(_REQUIRED_RENT_COLUMNS) - columns
    if missing:
        logger.warning(f"real_estate: {OVERRIDE_FILE} missing columns {sorted(missing)}")
        return []
    return rows


def _matching_overrides(city: str, state: str, latitude: float, longitude: float
                        ) -> List[Dict[str, object]]:
    matches: List[Dict[str, object]] = []
    for row in _load_overrides():
        row_city = (row.get("city") or "").strip().lower()
        row_state = (row.get("state") or "").strip().upper()
        if row_city and row_city != city.lower():
            continue
        if row_state and row_state != state.upper():
            continue
        row_lat = _parse_float(row.get("latitude"))
        row_lon = _parse_float(row.get("longitude"))
        radius = _parse_float(row.get("radius_miles"))
        rent = _parse_float(row.get("rent_per_sqft_annual"))
        if row_lat is None or row_lon is None or radius is None or rent is None:
            continue
        distance = haversine_miles((latitude, longitude), (row_lat, row_lon))
        if distance <= radius:
            matches.append({
                "row": row,
                "distance_miles": distance,
                "rent_per_sqft_annual": rent,
                # Optional columns — parsed if present, else None.
                "vacancy_rate_pct": _parse_float(row.get("vacancy_rate_pct")),
                "median_commercial_lease_term_months":
                    _parse_float(row.get("median_commercial_lease_term_months")),
            })
    matches.sort(key=lambda m: float(m["distance_miles"]))
    return matches


def collect_real_estate(city: str, state: str,
                        latitude: float, longitude: float
                        ) -> Dict[str, object]:
    """Return canonical shape, using a radius-matched override when present."""
    matches = _matching_overrides(city, state, latitude, longitude)
    if matches:
        match = matches[0]
        row = match["row"]
        rent = float(match["rent_per_sqft_annual"])
        source_url = (row.get("source_url") or "").strip()
        notes = (row.get("notes") or "").strip()
        values: Dict[str, object] = {
            "rent_per_sqft_month": round(rent / 12.0, 2),
            "rent_per_sqft_annual": rent,
            "vacancy_rate_pct": match.get("vacancy_rate_pct"),
            "median_commercial_lease_term_months":
                match.get("median_commercial_lease_term_months"),
            "rent_data_confidence": "manual_override",
            "rent_source": source_url or str(OVERRIDE_FILE),
            "rent_notes": notes,
        }
        populated_fields = [
            "rent_per_sqft_annual",
            "rent_per_sqft_month",
            "rent_data_confidence",
            "rent_source",
            "rent_notes",
        ]
        if values["vacancy_rate_pct"] is not None:
            populated_fields.append("vacancy_rate_pct")
        if values["median_commercial_lease_term_months"] is not None:
            populated_fields.append("median_commercial_lease_term_months")
        return {
            "values": values,
            "indicators": {
                "rent_override_distance_miles": round(
                    float(match["distance_miles"]), 3,
                ),
            },
            "sources": [{
                "name": "Manual rent overrides (data/raw/rent_overrides.csv)",
                "url": source_url or str(OVERRIDE_FILE),
                "fields": populated_fields,
                "collected_at": utcnow_iso(),
                "notes": (
                    f"user-supplied override matched within "
                    f"{float(match['distance_miles']):.2f} mi"
                ),
            }],
        }

    logger.debug(f"real_estate: no source for ({city}, {state}); returning unknowns")
    return {
        "values": _empty_values(),
        "indicators": {},
        "sources": [{
            "name": "Commercial real estate (not yet integrated)",
            "url": "",
            "fields": [],
            "collected_at": utcnow_iso(),
            "notes": "unknown — add cited rows to data/raw/rent_overrides.csv",
        }],
    }
