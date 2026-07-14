"""
Cache-first enrichment of PRIORITY ZIPs with the enhanced modeled signals.

Recomputes the three enhanced opportunity signals
(``healthcare_facility_density``, ``training_school_density``,
``competition_gap_score``) for the most important ZIPs — the curated finalist
spot-check list, the ZIPs used in the ZIP-demand reports, and the top dashboard
ZIPs — using ONLY data that already exists locally:

  * Full baseline ACS features from the cached Census bulk pull (offline).
  * ZIP land area from the row's population / population_density (the exact
    Census Gazetteer area density was derived from).
  * Google Places POI counts from the existing ``cache.sqlite`` — never a live
    paid call in the default (cache-only) mode.

Nothing is guessed. A signal that cannot be derived from cache is left missing
and the modeled weights renormalize (the BLS denominator stays at 0.80 instead
of reaching 1.00). The run prints a missing-info report, a summary table, and a
per-ZIP verification of raw counts → density → normalized → weight → contribution.

Default mode is cache-only (cost $0). ``--use-places`` would fill cache-missing
queries with LIVE Google Places calls (costs money); it is intentionally opt-in.

Reproduce:
    python -m scripts.enrich_priority_zips
    python -m scripts.enrich_priority_zips --zips 95112,07030
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import census_bulk
from app.config import (
    CACHE_DB,
    CACHE_ENABLED,
    COMPETITION_SATURATION_COUNT,
    PROCESSED_DIR,
    ZIP_MODELED_WEIGHTS_BLS,
)
from app.utils.cache import Cache
from app.scoring.enhanced_opportunity_signals import (
    _dedupe,
    compute_enhanced_signals,
)
from app.scoring.zip_modeled_opportunity import (
    compute_zip_modeled_opportunity,
    signal_weight_breakdown,
)
from app.utils.cache import build_cache_key
from scripts.enrich_top_zips import (
    ALL_PLACES_QUERIES,
    COMPETITOR_PLACES_QUERIES,
    HEALTHCARE_PLACES_QUERIES,
    PLACES_RADIUS_MILES,
    TRAINING_PLACES_QUERIES,
)

NATIONAL_DEMAND = PROCESSED_DIR / "national_demand.json"
OUTPUT = PROCESSED_DIR / "priority_zip_enrichment.json"
RADIUS_METERS = int(PLACES_RADIUS_MILES * 1609.34)

# Curated finalist spot-check list (data/processed/spot_check_zips.json).
SPOT_CHECK_ZIPS = ["07030", "10016", "94086", "11215", "94110",
                   "94538", "94541", "95112", "07514", "47809"]
# ZIPs used in the San Jose ZIP-demand reports.
ZIP_DEMAND_REPORT_ZIPS = ["95110", "95112", "95113", "95125", "95126",
                          "95133", "95192"]

ENHANCED_FIELDS = (
    "healthcare_facility_density",
    "training_school_density",
    "competition_gap_score",
)
# Which grouped query set feeds which enhanced signal.
CATEGORY_QUERIES = {
    "healthcare_facility_density": HEALTHCARE_PLACES_QUERIES,
    "training_school_density": TRAINING_PLACES_QUERIES,
    "competition_gap_score": COMPETITOR_PLACES_QUERIES,
}


class PlacesRunAborted(RuntimeError):
    """Raised on a Google Places error that is global, not per-ZIP, so the whole
    run should stop immediately rather than retry the same failure 26x.

    Two cases:
      * Quota (HTTP 429 / RESOURCE_EXHAUSTED): a full priority run needs far more
        Places calls than the per-day quota, so once quota is hit the rest will
        all 429 — wait for the daily reset or raise the quota.
      * Auth / billing (HTTP 403 PERMISSION_DENIED / REQUEST_DENIED / billing
        disabled): nothing will succeed until billing/the key is fixed.

    Aborting fast avoids emitting a wall of identical 'api_error' rows.
    """

    def __init__(self, message: str, kind: str = "fatal") -> None:
        super().__init__(message)
        self.kind = kind  # "quota" | "auth" | "fatal"


def _abort_kind(message: str) -> Optional[str]:
    """Classify a Places error message as a run-aborting condition, or None."""
    msg = (message or "").upper()
    if any(t in msg for t in (
        "429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE_LIMIT", "OVER_QUERY_LIMIT",
    )):
        return "quota"
    if any(t in msg for t in (
        "PERMISSION_DENIED", "REQUEST_DENIED", "BILLING", "API_KEY",
        "HTTP 403", "403", "401",
    )):
        return "auth"
    return None


# --------------------------------------------------------------------------- #
# Cache-only Places lookup
# --------------------------------------------------------------------------- #
def _cache_key(lat: float, lng: float, kwargs: Dict[str, Any]) -> str:
    params = {
        "latitude": lat,
        "longitude": lng,
        "radius_meters": RADIUS_METERS,
        "place_type": kwargs.get("place_type", "") or "",
        "keyword": kwargs.get("keyword", "") or "",
        "max_pages": 1,
    }
    return build_cache_key("google_places", "nearby_search", params)


def _cache_lookup(con: sqlite3.Connection, key: str) -> Optional[List[Dict[str, Any]]]:
    row = con.execute(
        "select value_json from cache_entries where key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


# --------------------------------------------------------------------------- #
# Per-ZIP enrichment
# --------------------------------------------------------------------------- #
def _baseline_features(zip_code: str,
                       acs_row: Dict[str, Any],
                       demand_row: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Optional[float]]:
    """Full baseline modeled features + ZIP land area, all from offline sources."""
    density = (demand_row or {}).get("population_density")
    population = acs_row.get("population")
    land_sqmi = (population / density
                 if population and density and density > 0 else None)
    feats = {
        "population": population,
        "population_density": density,
        "median_household_income": acs_row.get("median_household_income"),
        "working_age_share": acs_row.get("working_age_share"),
        "employment_rate": acs_row.get("employment_rate"),
        "bachelors_or_higher_share": acs_row.get("bachelors_or_higher_share"),
        "healthcare_employment_share": acs_row.get("healthcare_employment_share"),
    }
    return feats, land_sqmi


def enrich_one(
    con: sqlite3.Connection,
    zip_code: str,
    acs_row: Dict[str, Any],
    demand_row: Optional[Dict[str, Any]],
    client: Any = None,
) -> Dict[str, Any]:
    """Enrich one ZIP. ``client`` (InstrumentedPlacesClient) enables LIVE Places
    calls on cache misses (cache-first; existing cached queries are reused);
    ``client=None`` is cache-only (no calls, no cost)."""
    lat = (demand_row or {}).get("lat")
    lng = (demand_row or {}).get("lng")
    baseline, land_sqmi = _baseline_features(zip_code, acs_row, demand_row)
    if client is not None and hasattr(client, "begin_zip"):
        client.begin_zip(str(zip_code))

    category_counts: Dict[str, Optional[int]] = {}
    category_status: Dict[str, str] = {}
    cache_hits = 0
    cache_misses = 0
    live_calls = 0
    query_detail: Dict[str, Any] = {}

    for field, queries in CATEGORY_QUERIES.items():
        pois: List[Dict[str, Any]] = []
        hit_any = False
        had_error = False
        per_query = {}
        for tag, kwargs in queries:
            if lat is None or lng is None:
                per_query[tag] = "no_coordinates"
                cache_misses += 1
                continue
            if client is not None:
                # Cache-first; live call only on a miss (client accounts for it).
                try:
                    results = client.nearby_search(
                        (lat, lng), RADIUS_METERS, max_pages=1, **kwargs) or []
                    per_query[tag] = len(results)
                    hit_any = True
                    pois.extend(results)
                except Exception as exc:  # noqa: BLE001 — surface, don't crash
                    msg = str(exc)
                    per_query[tag] = f"api_error: {msg}"
                    had_error = True
                    kind = _abort_kind(msg)
                    if kind:
                        raise PlacesRunAborted(msg, kind=kind) from exc
                continue
            results = _cache_lookup(con, _cache_key(lat, lng, kwargs))
            if results is None:
                per_query[tag] = "cache_miss"
                cache_misses += 1
            else:
                per_query[tag] = len(results)
                cache_hits += 1
                hit_any = True
                pois.extend(results)
        query_detail[field] = per_query
        if not hit_any:
            category_counts[field] = None
            if lat is None or lng is None:
                category_status[field] = "no_coordinates"
            elif had_error:
                category_status[field] = "api_error"
            else:
                category_status[field] = "cache_missing"
        else:
            # Trust the targeted category queries (same as the production
            # enrich_zip_with_places path); dedupe by place_id so a POI returned
            # by two queries in the group counts once. Counts are a FLOOR for
            # dense ZIPs because each Places search caps at ~20 results.
            category_counts[field] = len(_dedupe(pois))
            category_status[field] = "ok"

    enhanced = compute_enhanced_signals(
        land_sqmi=land_sqmi,
        healthcare_count=category_counts["healthcare_facility_density"],
        training_count=category_counts["training_school_density"],
        competitor_count=category_counts["competition_gap_score"],
        saturation_count=COMPETITION_SATURATION_COUNT,
    )

    # Per-signal presence + reason.
    signal_report: Dict[str, Dict[str, Any]] = {}
    for field in ENHANCED_FIELDS:
        value = enhanced[field]
        if value is not None:
            status, reason = "present", ""
        elif category_status[field] == "no_coordinates":
            status, reason = "missing", "no coordinates for ZIP"
        elif category_status[field] == "api_error":
            status, reason = "missing", "live Google Places call failed (API error)"
        elif category_status[field] == "cache_missing":
            status = "missing"
            reason = "POI queries not in cache (needs live enrichment run)"
        elif land_sqmi is None and field != "competition_gap_score":
            status, reason = "missing", "ZIP land area unavailable (no population/density)"
        else:
            status, reason = "missing", "could not derive value"
        signal_report[field] = {
            "value": value,
            "status": status,
            "reason": reason,
            "raw_count": category_counts[field],
            "category_status": category_status[field],
        }

    # Merge enhanced signals into the full baseline and score.
    merged = dict(baseline)
    for field in ENHANCED_FIELDS:
        if enhanced[field] is not None:
            merged[field] = enhanced[field]

    scored = compute_zip_modeled_opportunity(merged)
    bd_full = signal_weight_breakdown(merged, ZIP_MODELED_WEIGHTS_BLS)
    bd_base = signal_weight_breakdown(baseline, ZIP_MODELED_WEIGHTS_BLS)

    # In live mode the client owns the authoritative per-ZIP call accounting.
    if client is not None:
        stats = getattr(client, "zip_stats", {}).get(str(zip_code), {})
        cache_hits = stats.get("cache_hits", 0)
        cache_misses = stats.get("cache_misses", 0)
        live_calls = stats.get("live_calls", 0)

    present = [f for f in ENHANCED_FIELDS if signal_report[f]["status"] == "present"]
    return {
        "zip": zip_code,
        "land_sqmi": (round(land_sqmi, 4) if land_sqmi is not None else None),
        "lat": lat,
        "lng": lng,
        "enhanced": {f: enhanced[f] for f in ENHANCED_FIELDS},
        "signal_report": signal_report,
        "query_detail": query_detail,
        "present_count": len(present),
        "baseline_denominator": bd_base["weight_used"],
        "final_denominator": bd_full["weight_used"],
        "overall": scored["overall"],
        "bls_demand": scored["bls_demand"],
        "cpr_demand": scored["cpr_demand"],
        "tier": scored["tier"],
        "weight_breakdown": bd_full["rows"],
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "live_calls": live_calls,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def print_missing_info_report(results: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 78)
    print("MISSING-INFO REPORT  (only signals that are NOT present)")
    print("=" * 78)
    header = f"{'ZIP':<7}{'missing signal':<28}{'status':<16}{'denom':<7} reason"
    print(header)
    print("-" * 78)
    any_missing = False
    for r in results:
        for field in ENHANCED_FIELDS:
            sr = r["signal_report"][field]
            if sr["status"] == "present":
                continue
            any_missing = True
            denom = (f"{r['final_denominator']:.2f}")
            print(f"{r['zip']:<7}{field:<28}{sr['category_status']:<16}"
                  f"{denom:<7} {sr['reason']}")
    if not any_missing:
        print("(no enhanced signals are missing for the priority ZIPs — all 3 "
              "present from cache)")
    print("-" * 78)
    # Always surface the broad-category cache gap, even when signals are present.
    print("Note: the broadened POI queries below are cache-missing for every ZIP "
          "(present\nsignals were computed from the cached hospital/urgent-care/"
          "nursing/competitor\nqueries only). Filling them needs a live "
          "--use-places run:")
    cached_tags = {"hospital", "urgent_care", "nursing_school", "cpr_bls_cert"}
    broad = [tag for tag, _ in ALL_PLACES_QUERIES if tag not in cached_tags]
    print("  " + ", ".join(broad))


def print_summary_table(results: List[Dict[str, Any]]) -> None:
    attempted = len(results)
    full = sum(1 for r in results if r["present_count"] == 3)
    partial = sum(1 for r in results if 0 < r["present_count"] < 3)
    none = sum(1 for r in results if r["present_count"] == 0)
    hits = sum(r["cache_hits"] for r in results)
    misses = sum(r["cache_misses"] for r in results)
    live_calls = sum(r.get("live_calls", 0) for r in results)
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    rows = [
        ("ZIPs attempted", attempted),
        ("Fully enhanced (all 3 signals)", full),
        ("Partially enhanced (1-2 signals)", partial),
        ("Still missing all enhanced signals", none),
        ("Google Places live calls made", live_calls),
        ("Cache hits", hits),
        ("Cache misses", misses),
    ]
    for label, value in rows:
        print(f"  {label:<38}{value}")


def print_sample_verification(results: List[Dict[str, Any]], zips: List[str]) -> None:
    print("\n" + "=" * 78)
    print("SAMPLE VERIFICATION")
    print("=" * 78)
    by_zip = {r["zip"]: r for r in results}
    for z in zips:
        r = by_zip.get(z)
        if not r:
            continue
        print(f"\nZIP {z}   land area = {_fmt(r['land_sqmi'])} sq mi   "
              f"overall={_fmt(r['overall'])} tier={r['tier']}")
        print(f"  baseline denominator {r['baseline_denominator']:.2f}  →  "
              f"final denominator {r['final_denominator']:.2f}")
        wb = {row["field"]: row for row in r["weight_breakdown"]}
        for field in ENHANCED_FIELDS:
            sr = r["signal_report"][field]
            row = wb.get(field, {})
            print(f"  {field}")
            print(f"      raw POI count : {_fmt(sr['raw_count'])}"
                  f"      value (feature): {_fmt(sr['value'])}")
            print(f"      normalized    : {_fmt(row.get('normalized'))}"
                  f"      weight: {_fmt(row.get('weight'))}"
                  f"      contribution: {_fmt(row.get('contribution'))}")


def select_priority_zips(explicit: Optional[List[str]], top_n: int,
                         demand_rows: List[Dict[str, Any]]) -> List[str]:
    if explicit:
        return explicit
    top = [r["zip"] for r in sorted(
        demand_rows, key=lambda r: r.get("overall") or 0, reverse=True)[:top_n]]
    ordered: List[str] = []
    seen = set()
    for z in SPOT_CHECK_ZIPS + ZIP_DEMAND_REPORT_ZIPS + top:
        if z not in seen:
            seen.add(z)
            ordered.append(z)
    return ordered


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zips", default="", help="Comma-separated ZIPs (overrides "
                    "the default priority set).")
    ap.add_argument("--top-n", type=int, default=15,
                    help="Also include the top-N dashboard ZIPs by overall score.")
    ap.add_argument("--input", default=str(NATIONAL_DEMAND))
    ap.add_argument("--output", default=str(OUTPUT))
    ap.add_argument("--acs-year", type=int, default=census_bulk.DEFAULT_ACS_YEAR)
    ap.add_argument("--use-places", action="store_true",
                    help="Make LIVE Google Places calls on cache misses (COSTS "
                         "MONEY). Default is cache-only / no calls.")
    ap.add_argument("--refresh-days", type=int, default=3650,
                    help="Reuse cached Places results fresher than this many days "
                         "(high default = reuse existing cache, only fetch new "
                         "queries live).")
    args = ap.parse_args(argv)

    print(f"Loading baseline rows: {args.input}")
    demand_rows = json.loads(Path(args.input).read_text())["rows"]
    by_zip = {r["zip"]: r for r in demand_rows}
    print(f"Loading cached ACS {args.acs_year} bulk (offline)…")
    from app.utils.cache import Cache
    acs = census_bulk.fetch_acs_zcta_bulk(args.acs_year, cache=Cache(CACHE_DB))

    explicit = [z.strip() for z in args.zips.split(",") if z.strip()] or None
    zips = select_priority_zips(explicit, args.top_n, demand_rows)
    print(f"Priority ZIPs ({len(zips)}): {', '.join(zips)}")

    client = None
    if args.use_places:
        from app.collectors.google_places import GooglePlacesClient
        from scripts.enrich_top_zips import InstrumentedPlacesClient
        cache = Cache(CACHE_DB) if CACHE_ENABLED else None
        print("LIVE Google Places enabled — cache-first, live only on misses "
              "(this spends API credits).")
        client = InstrumentedPlacesClient(
            GooglePlacesClient(cache=None), cache=cache,
            refresh_days=args.refresh_days, force_refresh=False)

    con = sqlite3.connect(CACHE_DB)
    results: List[Dict[str, Any]] = []
    for z in zips:
        acs_row = acs.get(z)
        if acs_row is None:
            results.append({
                "zip": z, "land_sqmi": None, "lat": None, "lng": None,
                "enhanced": {f: None for f in ENHANCED_FIELDS},
                "signal_report": {f: {"value": None, "status": "missing",
                                      "reason": "ZIP not in ACS bulk (no baseline)",
                                      "raw_count": None,
                                      "category_status": "no_baseline"}
                                  for f in ENHANCED_FIELDS},
                "query_detail": {}, "present_count": 0,
                "baseline_denominator": 0.0, "final_denominator": 0.0,
                "overall": None, "bls_demand": None, "cpr_demand": None,
                "tier": "baseline", "weight_breakdown": [],
                "cache_hits": 0, "cache_misses": 0, "live_calls": 0,
            })
            continue
        try:
            results.append(
                enrich_one(con, z, acs_row, by_zip.get(z), client=client))
        except PlacesRunAborted as exc:
            if exc.kind == "auth":
                hint = (
                    "Google Places returned a permission/billing error — every\n"
                    "        live call will fail until this is fixed. Most likely\n"
                    "        billing is disabled on the GCP project: re-enable it at\n"
                    "        https://console.cloud.google.com/project/_/billing/enable\n"
                    "        (also confirm the Places API is enabled and the key has\n"
                    "        no blocking application/API restrictions)."
                )
            else:  # quota
                hint = (
                    "Google Places daily quota exhausted — no further live calls\n"
                    "        will succeed today. Raise the quota (Cloud Console →\n"
                    "        APIs & Services → Quotas → Places API → SearchTextRequest\n"
                    "        per day) or wait for the daily reset (midnight US/Pacific)."
                )
            print(
                f"\n[ABORT] Stopped at ZIP {z}: {exc}\n        {hint}\n"
                f"        Already-cached ZIPs are unaffected.",
                file=sys.stderr,
            )
            break

    print_missing_info_report(results)
    print_summary_table(results)
    sample = [z for z in ("95112", "07030", "94538") if z in {r["zip"] for r in results}]
    print_sample_verification(results, sample or [r["zip"] for r in results[:3]])

    Path(args.output).write_text(json.dumps({"zips": zips, "results": results},
                                            indent=2))
    print(f"\nWrote per-ZIP enrichment detail → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
