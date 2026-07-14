"""
Build the national ZIP-level MODELED demand layer → data/processed/national_demand.json

This is an OFFLINE batch job (never run on web page load). It:
  1. Sources ~33k ZCTA centroids + land area from the public Census Gazetteer
     (reusing scripts/build_zip_centroids.parse_gazetteer_records).
  2. Pulls one bulk ACS ZCTA snapshot (app.collectors.census_bulk) — free,
     cached for a year.
  3. Scores every ZIP with app.scoring.zip_modeled_opportunity (public-data
     "baseline" tier).
  4. Writes a compact JSON the dashboard's "Modeled national demand" layer reads.

The output is a MODELED ESTIMATE, kept entirely separate from the real-history
layer (data/processed/latest_report.json). Phase-2 enrichment of selected ZIPs
plugs into the same score function and JSON shape — flipping rows to the
"enriched" tier — without changing the dashboard.

Examples
--------
    # Full national build (download gazetteer + bulk ACS, then score):
    python scripts/build_national_demand.py

    # Fast dev subset (no full national write):
    python scripts/build_national_demand.py --limit 500

    # Offline gazetteer, pinned ACS vintage:
    python scripts/build_national_demand.py --from-file ~/2024_Gaz_zcta_national.txt --acs-year 2022
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CACHE_DB, CACHE_ENABLED, PROCESSED_DIR, ZIP_MODEL_BOUNDS
from app.collectors import census_bulk
from app.scoring.zip_modeled_opportunity import compute_zip_modeled_opportunity
from app.utils.cache import Cache
from app.utils.logging_utils import get_logger
from scripts.build_zip_centroids import (
    fetch_gazetteer_text,
    gazetteer_url,
    parse_gazetteer_records,
)

logger = get_logger(__name__)

NATIONAL_DEMAND_FILE = PROCESSED_DIR / "national_demand.json"

# Canonical OPTIONAL Phase-2 enrichment fields a modeled ZIP row may carry.
# Phase 1 (baseline) omits them to keep the 33k-row file lean; an offline
# enrichment job (scripts/enrich_top_zips.py — not built yet) supplies them per
# high-priority ZIP. The dashboard renders whichever are present and never
# breaks on their absence. Descriptive fields below are passed through verbatim;
# any key that is also a scoring signal in ZIP_MODEL_BOUNDS (e.g.
# competition_gap_score, healthcare_facility_density) is additionally folded into
# the score so enriched ZIPs re-rank automatically — no formula rewrite.
ENRICHMENT_DESCRIPTIVE_FIELDS = (
    "healthcare_facility_count",
    "healthcare_poi_count",
    "medical_office_count",
    "urgent_care_count",
    "hospital_count",
    "nursing_school_count",
    "training_school_count",
    "community_facility_count",
    "college_count",
    "childcare_count",
    "ems_fire_count",
    "competitor_count",
    "enhanced_signal_debug",
    "competitor_density",
    "avg_competitor_rating",
    "competitor_schedule_count",
    "drive_time_access_score",
    "parking_score",
    "parking_proxy_score",
    "commercial_ready",
    "classroom_fit",
    "commercial_space_available",
    "estimated_rent",
    "rent_source",
)
ENRICHMENT_META_FIELDS = (
    "enrichment_tier",
    "enrichment_sources",
    "enrichment_updated_at",
)

METHODOLOGY = (
    "Modeled opportunity (0–100) estimated from public US Census ACS 5-year "
    "ZCTA demographics (population, density, income, working-age share, "
    "employment, education, healthcare-industry employment) and Census "
    "Gazetteer land area. It is a DECISION-SUPPORT ESTIMATE, not real "
    "enrollment history, and does not model AHA-vs-ARC brand preference — the "
    "course tilts reflect healthcare-workforce (BLS) vs community (CPR) demand "
    "propensity only. Validate any ZIP with a field test before leasing."
)


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return round(value, digits) if isinstance(value, (int, float)) else None


def features_for_zip(geo: Dict[str, float],
                     acs: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """Baseline modeled-score features from one ZIP's gazetteer + ACS records.

    Shared by the build and the Phase-2 enrich path so both score identically.
    Density = population ÷ land area (drops out when area is 0/unknown).
    """
    acs = acs or {}
    population = acs.get("population")
    land_sqmi = (geo or {}).get("land_sqmi") or 0.0
    density = (population / land_sqmi
               if population is not None and land_sqmi > 0 else None)
    return {
        "population": population,
        "population_density": density,
        "median_household_income": acs.get("median_household_income"),
        "working_age_share": acs.get("working_age_share"),
        "employment_rate": acs.get("employment_rate"),
        "bachelors_or_higher_share": acs.get("bachelors_or_higher_share"),
        "healthcare_employment_share": acs.get("healthcare_employment_share"),
    }


def _apply_enrichment(row: Dict[str, Any], enr: Optional[Dict[str, Any]]) -> None:
    """Attach an optional per-ZIP enrichment block's DISPLAY fields to the row.

    Phase-2 only. Descriptive fields (counts, rent, ratings) and metadata
    (tier/sources/updated_at) attach for display; signal-bearing fields are
    folded into the score separately, before scoring. Missing/None values are
    skipped — baseline rows are untouched.
    """
    if not enr:
        return
    for field in (*ENRICHMENT_DESCRIPTIVE_FIELDS, *ENRICHMENT_META_FIELDS):
        if enr.get(field) is not None:
            row[field] = enr[field]


def build_national_payload(
    gazetteer: Dict[str, Dict[str, float]],
    acs_by_zip: Dict[str, Dict[str, Optional[float]]],
    *,
    acs_vintage: int,
    limit: Optional[int] = None,
    enrichment_by_zip: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure assembly (no IO) so it is unit-testable without the network.

    Scores every ZIP that has BOTH a centroid and ACS data. Density is derived
    from population ÷ land area; when land area is 0/unknown the density signal
    simply drops out (never invented).

    ``enrichment_by_zip`` is the Phase-2 hook: ``{zip: {<enrichment fields>}}``.
    When provided for a ZIP, its descriptive fields attach to the row and its
    signal fields fold into the score (see :func:`_apply_enrichment`). Phase 1
    passes ``None`` and rows carry baseline fields only.
    """
    enrichment_by_zip = enrichment_by_zip or {}
    rows = []
    zips = sorted(set(gazetteer) & set(acs_by_zip))

    for zip_code in zips:
        if limit is not None and len(rows) >= limit:
            break
        geo = gazetteer[zip_code]
        acs = acs_by_zip[zip_code] or {}
        population = acs.get("population")
        density = features_for_zip(geo, acs)["population_density"]
        features = features_for_zip(geo, acs)
        enr = enrichment_by_zip.get(zip_code)
        # Signal-bearing enrichment is merged BEFORE scoring so the score updates.
        if enr:
            for key, value in enr.items():
                if key in ZIP_MODEL_BOUNDS and value is not None:
                    features[key] = value

        scored = compute_zip_modeled_opportunity(features)
        if scored["overall"] is None:
            continue  # no usable public signal — omit rather than show a fake 0

        row = {
            "zip": zip_code,
            "lat": geo["lat"],
            "lng": geo["lng"],
            "overall": scored["overall"],
            "bls_demand": scored["bls_demand"],
            "cpr_demand": scored["cpr_demand"],
            "tier": scored["tier"],
            "recommendation": scored["recommendation"],
            "score_drivers": scored["score_drivers"],
            "score_weaknesses": scored["score_weaknesses"],
            "plain_english_summary": scored["plain_english_summary"],
            "recommended_next_action": scored["recommended_next_action"],
            "risk_flags": scored["risk_flags"],
            "validation_score": scored.get("validation_score"),
            "validation_tier": scored.get("validation_tier"),
            "confidence_reason": scored.get("confidence_reason"),
            "recommendation_reason": scored.get("recommendation_reason"),
            "upgrade_reason": scored.get("upgrade_reason"),
            "downgrade_reason": scored.get("downgrade_reason"),
            "validation_signal_count": scored.get("validation_signal_count"),
            "validation_missing_signals": scored.get("validation_missing_signals"),
            "final_cap_applied": scored.get("final_cap_applied"),
            "cap_reason": scored.get("cap_reason"),
            "cap_details": scored.get("cap_details"),
            "validation_override_applied": scored.get("validation_override_applied"),
            "data_confidence": scored["data_quality"]["confidence"],
            "rationale": scored["rationale"],
            "population": _round(population, 0),
            "population_density": _round(density, 0),
            "land_sqmi": _round((geo or {}).get("land_sqmi") or None, 4),
            "median_income": _round(acs.get("median_household_income"), 0),
            "healthcare_employment_share": _round(
                acs.get("healthcare_employment_share"), 3),
        }
        # Attach descriptive enrichment fields (display-only) when present.
        _apply_enrichment(row, enr)
        rows.append(row)

    rows.sort(key=lambda r: (-(r["overall"] or 0), r["zip"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layer": "modeled_national_demand",
        "tier": "baseline",
        "acs_vintage": acs_vintage,
        "acs_label": f"ACS {acs_vintage} 5-year",
        "zip_count": len(rows),
        "methodology": METHODOLOGY,
        "rows": rows,
    }


def _source_gazetteer(args) -> Dict[str, Dict[str, float]]:
    if args.from_file:
        src = Path(args.from_file)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                text = zf.read(names[0]).decode("latin-1")
        else:
            text = src.read_text(encoding="latin-1")
        logger.info(f"Parsing local gazetteer {src}")
    else:
        url = args.url or gazetteer_url(args.gazetteer_year)
        logger.info(f"Downloading Census ZCTA gazetteer: {url}")
        text = fetch_gazetteer_text(url)
    return parse_gazetteer_records(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--acs-year", type=int, default=census_bulk.DEFAULT_ACS_YEAR,
                    help=f"ACS 5-year vintage (default {census_bulk.DEFAULT_ACS_YEAR}).")
    ap.add_argument("--gazetteer-year", type=int, default=2024,
                    help="Census Gazetteer vintage (default 2024).")
    ap.add_argument("--url", default="", help="Override gazetteer download URL.")
    ap.add_argument("--from-file", default="",
                    help="Parse a local Gazetteer .txt/.zip instead of downloading.")
    ap.add_argument("--output", default=str(NATIONAL_DEMAND_FILE),
                    help="Output JSON path (default data/processed/national_demand.json).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Score only the first N ZIPs (dev/test subset).")
    ap.add_argument("--no-cache", action="store_true",
                    help="Bypass the ACS cache and re-fetch.")
    args = ap.parse_args(argv)

    gazetteer = _source_gazetteer(args)
    if not gazetteer:
        logger.error("No ZCTA centroids parsed — aborting.")
        return 1
    logger.info(f"Gazetteer: {len(gazetteer)} ZCTAs.")

    cache = None if args.no_cache or not CACHE_ENABLED else Cache(CACHE_DB)
    acs = census_bulk.fetch_acs_zcta_bulk(args.acs_year, cache=cache)
    if not acs:
        logger.error("Bulk ACS pull returned nothing — aborting (check network "
                     "or CENSUS_API_KEY).")
        return 1

    payload = build_national_payload(
        gazetteer, acs, acs_vintage=args.acs_year, limit=args.limit
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    logger.info(f"Wrote {payload['zip_count']} modeled ZIPs → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
