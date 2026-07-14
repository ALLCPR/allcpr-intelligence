"""
HIFLD-style public facility collector → per-ZIP counts.

Parses public infrastructure point files (HIFLD / ArcGIS open data) for
hospitals, EMS stations, fire stations, and urgent care, then aggregates to ZIP.
These are free bulk downloads (no per-ZIP API cost, no 20-result cap that made
Google Places saturate), so they scale to all ~33k ZIPs offline.

Design:
  * ``parse_facility_csv`` is schema-tolerant (finds lat/lng/ZIP columns by name)
    and pure — unit-tested with tiny synthetic files.
  * ZIP assignment prefers an explicit ZIP column; otherwise it falls back to
    nearest ZCTA centroid (:mod:`app.geo.zcta_join`).
  * A missing file yields ``[]`` / ``{}`` — never a crash.

Real data: download the HIFLD "Hospitals", "Emergency Medical Service (EMS)
Stations", "Fire Stations", and an urgent-care source as CSV, drop them under
``data/raw/bulk/`` and point the build script at them (see
``scripts/build_bulk_enrichment.py``). URLs are documented there, not hit here.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.geo.zcta_join import (
    assign_point_to_zcta,
    build_grid_index,
)
from app.utils.geo_utils import haversine_miles
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Candidate column names (upper-cased) for the fields we need.
_LAT_COLS = ("LATITUDE", "LAT", "Y", "INTPTLAT")
_LNG_COLS = ("LONGITUDE", "LONG", "LON", "LNG", "X", "INTPTLONG")
_ZIP_COLS = ("ZIP", "ZIPCODE", "ZIP_CODE", "POSTAL", "POSTALCODE", "POSTAL_CODE")

# Category → which ZIP-level count it contributes to.
HEALTHCARE_CATEGORIES = ("hospital", "urgent_care")
EMS_FIRE_CATEGORIES = ("ems", "fire")


def _detect(header: List[str], candidates: Tuple[str, ...]) -> Optional[int]:
    upper = [h.strip().upper() for h in header]
    for cand in candidates:
        if cand in upper:
            return upper.index(cand)
    return None


def _num(value: Any) -> Optional[float]:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def parse_facility_csv(path: Path, category: str) -> List[Dict[str, Any]]:
    """Parse one facility CSV into ``[{category, lat, lng, zip}]``.

    Rows without usable coordinates AND without a ZIP are skipped. Missing file
    → ``[]``.
    """
    p = Path(path)
    if not p.exists():
        logger.warning(f"HIFLD: file not found, skipping: {p}")
        return []
    out: List[Dict[str, Any]] = []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return []
            i_lat = _detect(header, _LAT_COLS)
            i_lng = _detect(header, _LNG_COLS)
            i_zip = _detect(header, _ZIP_COLS)
            for row in reader:
                lat = _num(row[i_lat]) if i_lat is not None and i_lat < len(row) else None
                lng = _num(row[i_lng]) if i_lng is not None and i_lng < len(row) else None
                zip_code = None
                if i_zip is not None and i_zip < len(row):
                    z = str(row[i_zip]).strip()[:5].zfill(5)
                    if len(z) == 5 and z.isdigit():
                        zip_code = z
                if lat is None and lng is None and zip_code is None:
                    continue
                out.append({"category": category, "lat": lat, "lng": lng,
                            "zip": zip_code})
    except (OSError, csv.Error) as exc:
        logger.warning(f"HIFLD: failed to read {p}: {exc}")
        return []
    return out


def _nearest_hospital_miles(
    centroids: Dict[str, Tuple[float, float]],
    hospital_points: List[Tuple[float, float]],
) -> Dict[str, float]:
    """For each ZIP centroid, distance to the nearest hospital point (grid-indexed)."""
    if not hospital_points:
        return {}
    # Index hospitals on the same grid used for ZIP assignment.
    hosp_centroids = {str(i): pt for i, pt in enumerate(hospital_points)}
    index = build_grid_index(hosp_centroids)
    out: Dict[str, float] = {}
    for zip_code, (lat, lng) in centroids.items():
        nearest_id = assign_point_to_zcta(lat, lng, hosp_centroids, index=index,
                                          max_miles=100.0)
        if nearest_id is not None:
            out[zip_code] = round(
                haversine_miles((lat, lng), hosp_centroids[nearest_id]), 1)
    return out


def aggregate_hifld(
    facilities: List[Dict[str, Any]],
    centroids: Dict[str, Tuple[float, float]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate parsed facilities into ``{zip: {hospital_count, ...}}``.

    Each facility lands in a ZIP via its explicit ZIP column, else nearest
    centroid. Produces per-ZIP counts, ``healthcare_facility_count``/
    ``_density`` and ``nearest_hospital_miles``.
    """
    index = build_grid_index(centroids) if centroids else {}
    per_zip: Dict[str, Dict[str, int]] = {}
    hospital_points: List[Tuple[float, float]] = []

    for fac in facilities:
        zip_code = fac.get("zip")
        if not zip_code and centroids and fac.get("lat") is not None:
            zip_code = assign_point_to_zcta(fac["lat"], fac["lng"], centroids,
                                            index=index)
        if not zip_code:
            continue
        cat = fac["category"]
        bucket = per_zip.setdefault(zip_code, {})
        bucket[cat] = bucket.get(cat, 0) + 1
        if cat == "hospital" and fac.get("lat") is not None:
            hospital_points.append((fac["lat"], fac["lng"]))

    nearest = _nearest_hospital_miles(centroids, hospital_points)

    out: Dict[str, Dict[str, Any]] = {}
    for zip_code, cats in per_zip.items():
        hospital = cats.get("hospital", 0)
        urgent = cats.get("urgent_care", 0)
        ems_fire = cats.get("ems", 0) + cats.get("fire", 0)
        healthcare = hospital + urgent
        row: Dict[str, Any] = {
            "hospital_count": hospital,
            "urgent_care_count": urgent,
            "ems_fire_count": ems_fire,
            "healthcare_facility_count": healthcare,
            "healthcare_facility_density": float(healthcare),
        }
        if zip_code in nearest:
            row["nearest_hospital_miles"] = nearest[zip_code]
        out[zip_code] = row
    return out


def load_hifld(
    sources: Dict[str, Path],
    centroids: Dict[str, Tuple[float, float]],
) -> Dict[str, Dict[str, Any]]:
    """Parse every ``{category: csv_path}`` source and aggregate to ZIP.

    Missing sources are skipped with a warning. ``{}`` when nothing loads.
    """
    facilities: List[Dict[str, Any]] = []
    for category, path in sources.items():
        facilities.extend(parse_facility_csv(path, category))
    if not facilities:
        return {}
    return aggregate_hifld(facilities, centroids)
