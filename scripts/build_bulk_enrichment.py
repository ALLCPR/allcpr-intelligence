"""
Build per-ZIP enrichment from FREE BULK public datasets (offline batch).

Combines HIFLD facilities + NPI providers + IPEDS schools + OSM community
facilities into one per-ZIP table → ``data/processed/bulk_enrichment.json`` —
and merges it (DISPLAY-ONLY) into ``national_demand_enriched.json`` so the
dashboard can show it.

Why bulk, not Google Places: Places `nearby_search` caps dense ZIPs at ~20
results (saturates) and costs money per call. These public datasets are full,
free, downloadable once, and scale to all ~33k ZIPs with no per-ZIP API cost.
After the live Places scoring backtest moved overall correlation only 0.103 →
0.105, bulk datasets became the preferred path for national scoring improvement;
Places should stay finalist context unless a later backtest proves otherwise.

**Scoring gate.** New signals are attached for DISPLAY/CONTEXT only — they are
NOT folded into the modeled score by default. Promote a signal into scoring only
after `scripts/backtest_modeled_vs_historical.py` shows it improves
correlation/R² on real overlap data (same gate that kept Google Places off).

Usage:
    python scripts/build_bulk_enrichment.py --sample      # bundled tiny fixtures
    python scripts/build_bulk_enrichment.py               # real files in data/raw/bulk/

Real data (download once, drop under data/raw/bulk/):
  * HIFLD Hospitals / EMS Stations / Fire Stations / urgent care — ArcGIS open
    data CSV (https://hifld-geoplatform.hub.arcgis.com/).
  * NPPES NPI full file (https://download.cms.gov/nppes/NPI_Files.html) — ~9 GB,
    streamed (never fully loaded).
  * IPEDS HD institutions (https://nces.ed.gov/ipeds/use-the-data).
  * OSM: run your own Overpass query per region, save the JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.hifld_facilities import load_hifld
from app.collectors.ipeds import load_ipeds
from app.collectors.npi_bulk import aggregate_npi_by_zip
from app.collectors.osm_overpass_facilities import load_osm
from app.config import DATA_DIR, PROCESSED_DIR
from app.reports.commercial_validation import (
    COMMERCIAL_VALIDATION_FILE,
    load_commercial_summaries,
)
from app.reports.report_export import (
    NATIONAL_DEMAND_ENRICHED_PATH,
    NATIONAL_DEMAND_PATH,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

BULK_ENRICHMENT_PATH = PROCESSED_DIR / "bulk_enrichment.json"
SAMPLES = ROOT / "data" / "reference" / "bulk_samples"
RAW_BULK = DATA_DIR / "raw" / "bulk"

# Display-only descriptive fields produced by the bulk datasets.
BULK_FIELDS = (
    "hospital_count", "urgent_care_count", "ems_fire_count",
    "healthcare_facility_count", "healthcare_facility_density",
    "nearest_hospital_miles", "healthcare_provider_count", "nurse_count",
    "physician_count", "clinic_provider_count", "provider_density_per_10k_pop",
    "college_count", "nursing_school_count", "health_program_school_count",
    "student_enrollment_count", "childcare_count", "school_count",
    "community_facility_count", "community_facility_density",
    "parking_proxy_score", "commercial_access_proxy_score",
)


def _centroids_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for r in rows:
        z, lat, lng = r.get("zip"), r.get("lat"), r.get("lng")
        if z and lat is not None and lng is not None:
            out[str(z)] = (float(lat), float(lng))
    return out


def build_bulk_payload(
    national_rows: List[Dict[str, Any]],
    *,
    hifld_sources: Dict[str, Path],
    npi_path: Optional[Path],
    ipeds_path: Optional[Path],
    osm_path: Optional[Path],
    npi_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Pure assembly (collectors do their own IO) → bulk_enrichment payload.

    Combines every dataset into one per-ZIP dict and computes population-based
    densities. ZIPs with no bulk data at all are omitted.
    """
    centroids = _centroids_from_rows(national_rows)
    population = {str(r.get("zip")): r.get("population") for r in national_rows}

    hifld = load_hifld(hifld_sources, centroids) if hifld_sources else {}
    npi = aggregate_npi_by_zip(npi_path, limit=npi_limit) if npi_path else {}
    ipeds = load_ipeds(ipeds_path) if ipeds_path else {}
    osm = load_osm(osm_path, centroids) if osm_path else {}

    sources_used: List[str] = []
    if hifld:
        sources_used.append("HIFLD")
    if npi:
        sources_used.append("NPI")
    if ipeds:
        sources_used.append("IPEDS")
    if osm:
        sources_used.append("OSM")

    all_zips = set(hifld) | set(npi) | set(ipeds) | set(osm)
    now = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []
    for z in sorted(all_zips):
        row: Dict[str, Any] = {"zip": z}
        per_zip_sources: List[str] = []
        if z in hifld:
            row.update(hifld[z]); per_zip_sources.append("HIFLD")
        if z in npi:
            row.update(npi[z]); per_zip_sources.append("NPI")
        if z in ipeds:
            row.update(ipeds[z]); per_zip_sources.append("IPEDS")
        if z in osm:
            row.update(osm[z]); per_zip_sources.append("OSM")
        # Population-based densities.
        pop = population.get(z)
        if pop and row.get("healthcare_provider_count"):
            row["provider_density_per_10k_pop"] = round(
                row["healthcare_provider_count"] / pop * 10_000, 1)
        if "community_facility_count" in row:
            row.setdefault("community_facility_density",
                           float(row["community_facility_count"]))
        row["enrichment_sources"] = per_zip_sources
        row["enrichment_tier"] = "bulk_enriched"
        row["enrichment_updated_at"] = now
        rows.append(row)

    return {
        "generated_at": now,
        "layer": "bulk_enrichment",
        "sources": sources_used,
        "zip_count": len(rows),
        "rows": rows,
    }


def merge_bulk_into_national(
    national_payload: Dict[str, Any],
    bulk_rows: List[Dict[str, Any]],
    commercial_summaries: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Attach bulk (and commercial) fields onto national rows — DISPLAY ONLY.

    The modeled score (overall/bls/cpr) is intentionally NOT changed (scoring
    gate). Rows that gain enrichment flip ``tier`` to ``enriched``.
    """
    commercial_summaries = commercial_summaries or {}
    bulk_by_zip = {r["zip"]: r for r in bulk_rows}
    payload = dict(national_payload)
    enriched_zips: List[str] = []
    out_rows: List[Dict[str, Any]] = []
    for row in payload.get("rows") or []:
        row = dict(row)
        z = str(row.get("zip"))
        bulk = bulk_by_zip.get(z)
        if bulk:
            for f in BULK_FIELDS:
                if f in bulk and bulk[f] is not None:
                    row[f] = bulk[f]
            row["tier"] = "enriched"
            row["enrichment_tier"] = bulk.get("enrichment_tier", "bulk_enriched")
            row["enrichment_sources"] = bulk.get("enrichment_sources") or []
            row["enrichment_updated_at"] = bulk.get("enrichment_updated_at")
            enriched_zips.append(z)
        summary = commercial_summaries.get(z)
        if summary and summary.get("commercial_validated"):
            row["commercial"] = summary
            if z not in enriched_zips:
                enriched_zips.append(z)
        out_rows.append(row)
    payload["rows"] = out_rows
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["enriched_zip_count"] = len(set(enriched_zips))
    return payload


def _resolve_sources(args) -> Tuple[Dict[str, Path], Optional[Path], Optional[Path], Optional[Path]]:
    base = SAMPLES if args.sample else RAW_BULK
    if args.sample:
        hifld = {"hospital": base / "hifld_hospitals.csv",
                 "ems": base / "hifld_ems_fire.csv"}
        return hifld, base / "npi_sample.csv", base / "ipeds.csv", base / "osm_facilities.json"
    # Real mode: documented expected filenames under data/raw/bulk/ (skip absent).
    hifld = {"hospital": base / "hifld_hospitals.csv",
             "urgent_care": base / "hifld_urgent_care.csv",
             "ems": base / "hifld_ems.csv",
             "fire": base / "hifld_fire.csv"}
    hifld = {k: v for k, v in hifld.items() if v.exists()}
    npi = base / "npidata.csv"
    ipeds = base / "ipeds_hd.csv"
    osm = base / "osm_facilities.json"
    return hifld, (npi if npi.exists() else None), (ipeds if ipeds.exists() else None), (osm if osm.exists() else None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true",
                    help="Use bundled tiny fixtures (offline, deterministic).")
    ap.add_argument("--national", default=str(NATIONAL_DEMAND_PATH))
    ap.add_argument("--bulk-output", default=str(BULK_ENRICHMENT_PATH))
    ap.add_argument("--enriched-output", default=str(NATIONAL_DEMAND_ENRICHED_PATH))
    ap.add_argument("--npi-limit", type=int, default=None,
                    help="Cap NPPES rows processed (dev).")
    args = ap.parse_args(argv)

    nat_path = Path(args.national)
    if not nat_path.exists():
        logger.error(f"National baseline not found: {nat_path}. "
                     f"Run build_national_demand.py first.")
        return 1
    national = json.loads(nat_path.read_text(encoding="utf-8"))

    hifld_sources, npi_path, ipeds_path, osm_path = _resolve_sources(args)
    bulk = build_bulk_payload(
        national.get("rows") or [], hifld_sources=hifld_sources,
        npi_path=npi_path, ipeds_path=ipeds_path, osm_path=osm_path,
        npi_limit=args.npi_limit)
    Path(args.bulk_output).write_text(json.dumps(bulk), encoding="utf-8")
    logger.info(f"Bulk enrichment: {bulk['zip_count']} ZIPs from "
                f"{bulk['sources'] or 'no sources'} → {args.bulk_output}")

    merged = merge_bulk_into_national(
        national, bulk["rows"],
        load_commercial_summaries(COMMERCIAL_VALIDATION_FILE))
    Path(args.enriched_output).write_text(json.dumps(merged), encoding="utf-8")
    logger.info(f"Merged (display-only) → {args.enriched_output} "
                f"({merged.get('enriched_zip_count')} enriched ZIPs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
