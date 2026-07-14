"""
Patch the corrected cache-based enhanced signals into the dashboard source file.

Replaces the OLD raw-count values of ``healthcare_facility_density`` /
``training_school_density`` (and refreshes ``competition_gap_score``) in
``national_demand_enriched.json`` for the fully-enhanced priority ZIPs produced
by :mod:`scripts.enrich_priority_zips`, then rescore them exactly the way the
production build + enrich passes do (compute_zip_modeled_opportunity →
automated_validation → rural caps).

Only ZIPs with all three real, cache-derived signals are touched. Missing ZIPs
and missing signals are left untouched — never guessed. Default is a DRY RUN
(prints before/after, writes nothing); pass ``--apply`` to write the file.

After applying, regenerate the served artifacts:
    python -m scripts.build_lite_outputs --details
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import census_bulk
from app.config import PROCESSED_DIR, ZIP_MODELED_WEIGHTS_BLS
from app.scoring.zip_modeled_opportunity import (
    automated_validation,
    compute_zip_modeled_opportunity,
    signal_weight_breakdown,
    _rural_market_caps,
)
from app.utils.cache import Cache
from app.config import CACHE_DB

ENRICHED = PROCESSED_DIR / "national_demand_enriched.json"
PRIORITY = PROCESSED_DIR / "priority_zip_enrichment.json"
ENHANCED_FIELDS = (
    "healthcare_facility_density",
    "training_school_density",
    "competition_gap_score",
)
SCORE_FIELDS = (
    "overall", "bls_demand", "cpr_demand", "tier", "recommendation",
    "score_drivers", "score_weaknesses", "plain_english_summary",
    "recommended_next_action", "risk_flags", "rationale",
)
SAMPLES = ("95112", "07030", "94538")


def _baseline(acs_row: Dict[str, Any], density: Any) -> Dict[str, Any]:
    return {
        "population": acs_row.get("population"),
        "population_density": density,
        "median_household_income": acs_row.get("median_household_income"),
        "working_age_share": acs_row.get("working_age_share"),
        "employment_rate": acs_row.get("employment_rate"),
        "bachelors_or_higher_share": acs_row.get("bachelors_or_higher_share"),
        "healthcare_employment_share": acs_row.get("healthcare_employment_share"),
    }


def _denominator(baseline: Dict[str, Any], enhanced: Dict[str, Any]) -> float:
    feats = dict(baseline)
    for f in ENHANCED_FIELDS:
        if enhanced.get(f) is not None:
            feats[f] = enhanced[f]
    return signal_weight_breakdown(feats, ZIP_MODELED_WEIGHTS_BLS)["weight_used"]


def patch_row(row: Dict[str, Any], pr: Dict[str, Any],
              acs_row: Dict[str, Any]) -> Dict[str, Any]:
    """Patch one enriched row in place; return a before/after record."""
    density = row.get("population_density")
    baseline = _baseline(acs_row, density)

    old_enh = {f: row.get(f) for f in ENHANCED_FIELDS}
    new_enh = {f: pr["enhanced"][f] for f in ENHANCED_FIELDS}
    before = {
        "overall": row.get("overall"),
        "denominator": _denominator(baseline, old_enh),
        **{f: old_enh[f] for f in ENHANCED_FIELDS},
    }

    # 1) Corrected enhanced signals + matching descriptive counts.
    for f in ENHANCED_FIELDS:
        row[f] = new_enh[f]
    sr = pr["signal_report"]
    row["healthcare_facility_count"] = sr["healthcare_facility_density"]["raw_count"]
    row["training_school_count"] = sr["training_school_density"]["raw_count"]
    row["competitor_count"] = sr["competition_gap_score"]["raw_count"]
    row["land_sqmi"] = pr.get("land_sqmi")
    row["enhanced_signal_debug"] = {
        "land_sqmi": pr.get("land_sqmi"),
        "source": "cache_recompute",
        "note": ("densities normalized per sq mile from cached hospital/"
                 "urgent-care/nursing/competitor Places queries; broadened "
                 "categories pending a live run"),
    }

    # 2) Rescore from full baseline + corrected enhanced (build pass).
    feats = dict(baseline)
    for f in ENHANCED_FIELDS:
        if new_enh[f] is not None:
            feats[f] = new_enh[f]
    scored = compute_zip_modeled_opportunity(feats)
    for f in SCORE_FIELDS:
        row[f] = scored[f]
    row["data_confidence"] = scored["data_quality"]["confidence"]

    # 3) Validation + rural caps over the full row (enrich write pass).
    confidence = str(row.get("data_confidence") or "missing")
    validation = automated_validation(row, row.get("overall"), confidence)
    caps = _rural_market_caps(row, overall=row.get("overall"),
                              bls=row.get("bls_demand"), cpr=row.get("cpr_demand"),
                              validation=validation)
    row["overall"] = caps["overall"]
    row["bls_demand"] = caps["bls_demand"]
    row["cpr_demand"] = caps["cpr_demand"]
    validation = automated_validation(row, row.get("overall"), confidence)
    row.update(validation)
    row["final_cap_applied"] = caps["final_cap_applied"]
    row["cap_reason"] = caps["cap_reason"]
    row["cap_details"] = caps["cap_details"]
    row["validation_override_applied"] = caps["validation_override_applied"]
    row["recommendation"] = validation.get("validation_tier") or row.get("recommendation")
    row["tier"] = "enriched"
    row["enrichment_updated_at"] = datetime.now(timezone.utc).isoformat()
    sources = sorted(set(list(row.get("enrichment_sources") or []) + ["google_places"]))
    row["enrichment_sources"] = sources

    after = {
        "overall": row.get("overall"),
        "denominator": _denominator(baseline, new_enh),
        **{f: row.get(f) for f in ENHANCED_FIELDS},
    }
    return {"zip": row.get("zip"), "before": before, "after": after}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Write the patched file (default: dry run).")
    ap.add_argument("--enriched", default=str(ENRICHED))
    ap.add_argument("--priority", default=str(PRIORITY))
    ap.add_argument("--acs-year", type=int, default=census_bulk.DEFAULT_ACS_YEAR)
    args = ap.parse_args(argv)

    priority = json.loads(Path(args.priority).read_text())
    targets = {r["zip"]: r for r in priority["results"] if r["present_count"] == 3}
    print(f"Fully-enhanced priority ZIPs to patch: {len(targets)}")
    print(f"  {', '.join(sorted(targets))}")

    print(f"Loading cached ACS {args.acs_year} bulk…")
    acs = census_bulk.fetch_acs_zcta_bulk(args.acs_year, cache=Cache(CACHE_DB))

    print(f"Loading {args.enriched}…")
    payload = json.loads(Path(args.enriched).read_text())
    by_zip = {r.get("zip"): r for r in payload.get("rows") or []}

    diffs: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for z, pr in targets.items():
        row = by_zip.get(z)
        acs_row = acs.get(z)
        if row is None or acs_row is None:
            skipped.append(z)
            continue
        diffs.append(patch_row(row, pr, acs_row))

    if skipped:
        print(f"Skipped (not in enriched file or ACS): {skipped}")
    print(f"Patched {len(diffs)} rows.\n")

    print("=" * 90)
    print("BEFORE / AFTER  (samples)")
    print("=" * 90)
    by = {d["zip"]: d for d in diffs}
    for z in SAMPLES:
        d = by.get(z)
        if not d:
            continue
        b, a = d["before"], d["after"]
        print(f"\nZIP {z}")
        print(f"  overall score          : {b['overall']}  ->  {a['overall']}")
        print(f"  denominator (BLS)      : {b['denominator']:.2f}  ->  {a['denominator']:.2f}")
        print(f"  healthcare_facility_density : {b['healthcare_facility_density']}  ->  {a['healthcare_facility_density']}")
        print(f"  training_school_density     : {b['training_school_density']}  ->  {a['training_school_density']}")
        print(f"  competition_gap_score       : {b['competition_gap_score']}  ->  {a['competition_gap_score']}")

    if args.apply:
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        payload["priority_signal_patch"] = {
            "patched_at": datetime.now(timezone.utc).isoformat(),
            "patched_zips": sorted(by.keys()),
            "note": "Recomputed healthcare/training density (per sq mi) + "
                    "competition gap from cache; rescored.",
        }
        Path(args.enriched).write_text(json.dumps(payload))
        print(f"\nAPPLIED → wrote {args.enriched}")
        print("Next: python -m scripts.build_lite_outputs --details")
    else:
        print("\nDRY RUN — no file written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
