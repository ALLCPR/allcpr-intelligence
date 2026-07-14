"""
End-to-end pipeline:
  targets -> candidates -> anchor + demand/competition/economy/accessibility
  enrichment -> per-candidate scoring -> CSV + Markdown + JSON (+ optional
  HTML) reports.

Modes:
  - single_address: grid around one or more address-like targets
  - city: Phase 1 city grid behavior
  - metro_comparison: one coarse candidate per city/neighborhood/corridor,
    with city-level and candidate-level ranking
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import bls_or_labor as _bls_or_labor
from app.collectors import census as _census
from app.collectors import enrollware as _enrollware
from app.collectors import job_postings as _job_postings
from app.collectors import mapbox_isochrones as _mapbox
from app.collectors import openrouteservice_isochrones as _ors
from app.collectors import osm_zoning as _osm_zoning
from app.collectors.google_places import GooglePlacesClient
from app.config import (
    CACHE_DB,
    CACHE_ENABLED,
    DEFAULT_RADIUS_MILES,
    ENRICHED_DIR,
    GRID_SPACING_MILES,
    MAX_CANDIDATES_PER_CITY,
    METRO_DEDUPE_DISTANCE_MILES,
    REPORTS_DIR,
    SCORED_DIR,
)
from app.utils.cache import Cache
from app.enrichers.area_profile import build_area_profile
from app.enrichers.historical_performance import build_candidate_historical_performance
from app.scoring import zip_demand as _zip_demand
from app.reports.csv_report import candidate_to_row, write_csv_report
from app.reports.html_report import write_html_report
from app.reports import ai_summary as _ai_summary
from app.reports.interpretation import (
    STRATEGY_KEYS,
    aggregate_demand_counts,
    build_course_performance_section,
    build_report_interpretation,
    candidate_matches_strategies,
)
from app.reports.json_report import render_json, write_json_report
from app.reports.report_export import LATEST_REPORT_PATH, write_latest_report_json
from app.reports.markdown_report import (
    render_markdown_report,
    render_metro_comparison_report,
    write_markdown_report,
)
from app.scoring.cohort_normalization import (
    apply_cohort_confidence,
    apply_cohort_normalization,
)
from app.scoring.rent_estimate import apply_rent_estimates
from app.scoring.site_score import score_profile
from app.utils.candidate_dedup import deduplicate_ranked_candidates
from app.utils.catchment_filter import apply_catchment_filter
from app.utils.commercial_zones import (
    bbox_for_radius,
    filter_points_to_commercial,
)
from app.utils.density_probe import probe_density
from app.utils.csv_utils import load_lines
from app.utils.geo_utils import generate_grid, geocode_city
from app.utils.logging_utils import get_logger

logger = get_logger("full_pipeline")


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("single_address", "city", "metro_comparison"),
                    default="city",
                    help="Analysis mode. city preserves Phase 1 grid behavior; "
                         "metro_comparison compares coarse areas instead of "
                         "dense nearby grid points.")
    ap.add_argument("--cities", required=True,
                    help="Path to a text file with one target per line. "
                         "Each line may be a city, address, neighborhood, "
                         "or corridor; append ', ST' when possible.")
    ap.add_argument("--state", default="",
                    help="Default state code if not embedded in the target line.")
    ap.add_argument("--radius-miles", type=float, default=DEFAULT_RADIUS_MILES,
                    help="Search radius around each candidate point.")
    ap.add_argument("--grid-spacing-miles", type=float, default=GRID_SPACING_MILES,
                    help="Grid spacing between candidate points.")
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES_PER_CITY,
                    help="Cap on candidate points per target.")
    ap.add_argument("--output", default=str(REPORTS_DIR / "allcpr_site_report.md"),
                    help="Output Markdown report path. If multiple non-metro "
                         "targets are provided, this is treated as a directory.")
    ap.add_argument("--csv-output",
                    default=str(SCORED_DIR / "scored_locations.csv"),
                    help="Output CSV path for the flat scored table.")
    ap.add_argument("--json-output",
                    default=str(SCORED_DIR / "scored_locations.json"),
                    help="Output JSON path for the structured scored data.")
    ap.add_argument("--html-output", default="",
                    help="Optional output HTML report path.")
    ap.add_argument("--dashboard-json", default=str(LATEST_REPORT_PATH),
                    help="Output path for the web-dashboard JSON payload "
                         "(written only when --html-output is set).")
    ap.add_argument("--report-style",
                    choices=("executive", "detailed", "debug"),
                    default="executive",
                    help="Markdown/HTML presentation style. 'executive' "
                         "(default) is concise and decision-ready; 'detailed' "
                         "keeps the full rich tables; 'debug' adds the raw "
                         "per-field source audit and diagnostics. JSON/CSV "
                         "always keep the full detailed data regardless.")
    ap.add_argument("--fit-strategy", default="",
                    help="Opt-in report filter. Comma-separated strategy "
                         "keys; only areas whose recommended go-to-market "
                         "strategy matches are shown in the Markdown/HTML "
                         "report. Keys: "
                         + ", ".join(sorted(STRATEGY_KEYS.values()))
                         + ". JSON/CSV always keep every evaluated area.")
    ap.add_argument("--cache-mode",
                    choices=("auto", "no-cache", "force-refresh"),
                    default="auto",
                    help="Response-cache behavior. 'auto' (default) honors "
                         "per-source TTLs; 'no-cache' bypasses entirely; "
                         "'force-refresh' ignores existing entries and "
                         "overwrites them.")
    ap.add_argument("--purge-stale-cache", action="store_true",
                    help="After the run, delete every cache entry past its "
                         "TTL. Housekeeping; safe to run any time.")
    ap.add_argument("--dedupe-distance-miles", type=float, default=None,
                    help="Deduplicate candidates closer than this. Defaults to "
                         f"{METRO_DEDUPE_DISTANCE_MILES} in metro_comparison "
                         "and 0 in other modes.")
    ap.add_argument("--analyze-competitor-websites",
                    dest="analyze_competitor_websites",
                    action="store_true",
                    default=None,
                    help="Fetch competitor homepage + one obvious classes/booking page.")
    ap.add_argument("--skip-competitor-websites",
                    dest="analyze_competitor_websites",
                    action="store_false",
                    help="Skip competitor website analysis for this run.")
    ap.add_argument("--analyze-reviews", action="store_true",
                    help="Fetch Yelp review excerpts for matched competitors "
                         "and cluster recurring complaint themes (parking, "
                         "scheduling, instructor quality, etc.) into a "
                         "'why competitors fail' positioning report. "
                         "Requires YELP_API_KEY; ~1 extra Yelp call per "
                         "competitor (capped).")
    ap.add_argument("--keep-non-viable", action="store_true",
                    help="Keep candidates whose anchor is industrial-only / "
                         "non-commercial. Default is to drop them from the "
                         "ranked report (they're still tracked in JSON).")
    ap.add_argument("--cohort-blend", type=float, default=0.5,
                    help="How much weight to give cohort-relative scores "
                         "vs absolute (0.0=absolute only, 1.0=cohort only). "
                         "Default 0.5. Only applies when more than one "
                         "candidate is evaluated.")
    ap.add_argument("--no-cohort-normalization", action="store_true",
                    help="Disable cohort normalization entirely (use absolute "
                         "scores only). Useful for single-candidate runs or "
                         "for comparing the legacy scoring behavior.")
    ap.add_argument("--no-osm-zoning", action="store_true",
                    help="Skip OSM commercial-zone filtering of candidate "
                         "grid points. Useful when Overpass is rate-limited "
                         "or for testing without OSM dependency.")
    ap.add_argument("--osm-zone-buffer-meters", type=float, default=250.0,
                    help="A candidate grid point is kept if it lies inside "
                         "OR within this many meters of a commercial polygon "
                         "(default 250m). Higher values are more permissive.")
    ap.add_argument("--catchment-minutes", type=int, default=0,
                    help="When >0 and MAPBOX_TOKEN is set, fetch a "
                         "drive-time isochrone of this many minutes per "
                         "candidate and post-filter demand/competitor "
                         "results to those inside the catchment polygon. "
                         "0 disables (uses circular radius only).")
    ap.add_argument("--catchment-profile",
                    choices=("driving", "driving-traffic", "walking", "cycling"),
                    default="driving",
                    help="Mapbox routing profile for the isochrone. Default "
                         "is 'driving'. 'driving-traffic' uses live traffic.")
    ap.add_argument("--no-dense-mode", action="store_true",
                    help="Disable dense-metro auto-detection. Default "
                         "behavior probes CPR competitor density at each "
                         "target's configured radius and auto-reduces "
                         "radius + grid spacing when density exceeds the "
                         "dense-metro threshold (so SF/NYC/LA don't blend "
                         "neighborhoods together).")
    ap.add_argument("--dense-threshold", type=int, default=20,
                    help="Number of CPR competitors within the configured "
                         "radius that triggers dense-metro mode. Default 20.")
    ap.add_argument("--save-profiles", action="store_true",
                    help="Also persist raw enriched profiles as JSON in data/enriched/")
    ap.add_argument("--enrollware-file", default="",
                    help="Path to an Enrollware class-history export (.xlsx / "
                         ".csv) for the Phase 4B course-performance sections. "
                         "Defaults to data/raw/Enrollware Data - Classes.xlsx "
                         "or data/raw/enrollware_classes.{xlsx,csv} when "
                         "present.")
    ap.add_argument("--enrollware-locations-file", default="",
                    help="Optional Enrollware Locations export (.xlsx / .csv) "
                         "used to resolve class-location abbreviations into "
                         "city/state. Defaults to data/raw/Enrollware Data - "
                         "Locations.xlsx or data/raw/enrollware_locations.* "
                         "when present.")
    ap.add_argument("--no-enrollware", action="store_true",
                    help="Skip the Phase 4B course-performance evaluation even "
                         "if an Enrollware export is present.")
    ap.add_argument("--no-ai-summary", action="store_true",
                    help="Skip the optional AI executive summary even when an "
                         "LLM provider (GROQ_API_KEY / OPENAI_API_KEY) is "
                         "configured. The summary only rephrases the "
                         "deterministic report; it never adds figures.")
    return ap.parse_args(argv)


def parse_city_line(line: str, default_state: str) -> Tuple[str, str]:
    """Backward-compatible alias for old tests/scripts."""
    return parse_target_line(line, default_state)


def parse_target_line(line: str, default_state: str) -> Tuple[str, str]:
    """Parse a target line, preserving commas in corridor/neighborhood names."""
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) >= 2 and len(parts[-1]) == 2:
        return ", ".join(parts[:-1]), parts[-1].upper()
    if "," in line:
        first, rest = [p.strip() for p in line.split(",", 1)]
        if len(rest) == 2:
            return first, rest.upper()
    return line.strip(), default_state.upper()


def _strip_internal(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unserializable raw objects from a profile before persisting."""
    return {
        k: v for k, v in profile.items()
        if k not in ("anchor_obj", "demand_top_places_obj", "competitors_obj")
    }


def _candidate_points_for_mode(
    mode: str,
    center: Tuple[float, float],
    radius_miles: float,
    grid_spacing_miles: float,
    max_candidates: int,
) -> List[Tuple[float, float]]:
    if mode == "metro_comparison":
        return [center]
    grid_radius = radius_miles if mode == "single_address" else radius_miles * 1.5
    return generate_grid(
        center,
        radius_miles=grid_radius,
        spacing_miles=grid_spacing_miles,
        max_points=max_candidates,
    )


def _site_score(scored: Dict[str, Any]) -> float:
    """Ranking number. Prefers area_score (always present); falls back to the
    gated site_score (None unless validated) only for legacy inputs."""
    value = scored.get("area_score")
    if not isinstance(value, (int, float)):
        value = scored.get("site_score")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _parse_fit_keys(raw: str) -> set:
    """Validate the comma-separated --fit-strategy keys against known keys."""
    keys = {k.strip().lower() for k in (raw or "").split(",") if k.strip()}
    valid = set(STRATEGY_KEYS.values())
    unknown = keys - valid
    if unknown:
        logger.warning(
            f"Ignoring unknown --fit-strategy keys {sorted(unknown)}; "
            f"valid keys: {sorted(valid)}"
        )
    return keys & valid


def _filter_fit(ranked: List[Tuple[Dict, Dict]], fit_keys: set
                ) -> List[Tuple[Dict, Dict]]:
    """Keep only candidates whose recommended strategy matches `fit_keys`.

    An empty `fit_keys` is a no-op — every candidate is kept.
    """
    if not fit_keys:
        return ranked
    return [
        (profile, scored) for profile, scored in ranked
        if candidate_matches_strategies(profile, scored, fit_keys)
    ]


def _filter_viable(ranked: List[Tuple[Dict, Dict]]) -> List[Tuple[Dict, Dict]]:
    """Drop candidates whose anchor was flagged as non-commercial."""
    out: List[Tuple[Dict, Dict]] = []
    dropped: List[str] = []
    for profile, scored in ranked:
        viability = profile.get("viability") or {}
        if viability.get("viable", True):
            out.append((profile, scored))
        else:
            name = (profile.get("anchor") or {}).get("name") \
                or profile.get("candidate_name") or profile.get("candidate_id")
            dropped.append(f"{name} ({viability.get('reason')})")
    if dropped:
        logger.info(
            f"Dropped {len(dropped)} non-viable candidate(s): "
            f"{dropped[:5]}{'…' if len(dropped) > 5 else ''}"
        )
    return out


def _assign_candidate_ranks(ranked: List[Tuple[Dict, Dict]]) -> None:
    for idx, (profile, _) in enumerate(ranked, start=1):
        profile["candidate_rank"] = idx


def _assign_score_deltas(ranked: List[Tuple[Dict, Dict]]) -> None:
    """Annotate each candidate with score_delta_vs_mean + a coarse label.

    Dense urban runs typically produce site_scores within 3-5 points of each
    other — the absolute number alone reads "they all look the same."
    Surfacing the delta against the cohort mean lets the report show which
    candidates are *meaningfully* above or below the pack.
    """
    scores = [_site_score(s) for _, s in ranked]
    if not scores:
        return
    mean = sum(scores) / len(scores)
    spread = max(scores) - min(scores) if len(scores) > 1 else 0.0
    for (profile, scored), score in zip(ranked, scores):
        delta = round(score - mean, 2)
        if spread < 1.0 or abs(delta) < 1.0:
            label = "Around cohort mean"
        elif delta >= 3.0:
            label = "Well above cohort mean"
        elif delta >= 1.0:
            label = "Above cohort mean"
        elif delta <= -3.0:
            label = "Well below cohort mean"
        else:
            label = "Below cohort mean"
        profile["score_delta_vs_mean"] = delta
        profile["score_delta_label"] = label
        profile["cohort_mean_site_score"] = round(mean, 2)


def _build_city_rankings(ranked: List[Tuple[Dict, Dict]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Tuple[Dict, Dict]]] = {}
    for profile, scored in ranked:
        area = str(profile.get("comparison_area") or profile.get("city") or "unknown")
        grouped.setdefault(area, []).append((profile, scored))

    rows: List[Dict[str, Any]] = []
    for area, items in grouped.items():
        items.sort(key=lambda ps: _site_score(ps[1]), reverse=True)
        top_scores = [_site_score(s) for _, s in items[:3]]
        best_profile, best_scored = items[0]
        best_anchor = (best_profile.get("anchor") or {}).get("name") \
            or best_profile.get("candidate_name") or best_profile.get("candidate_id")
        rows.append({
            "area": area,
            "best_candidate": best_anchor,
            "best_site_score": _site_score(best_scored),
            "avg_top_site_score": (
                round(sum(top_scores) / len(top_scores), 2) if top_scores else 0.0
            ),
            "candidate_count": len(items),
        })

    rows.sort(
        key=lambda r: (float(r["best_site_score"]), float(r["avg_top_site_score"])),
        reverse=True,
    )
    area_to_rank: Dict[str, int] = {}
    for idx, row in enumerate(rows, start=1):
        row["city_rank"] = idx
        area_to_rank[str(row["area"])] = idx

    for profile, _ in ranked:
        area = str(profile.get("comparison_area") or profile.get("city") or "unknown")
        profile["city_rank"] = area_to_rank.get(area, "unknown")
    return rows


def _markdown_output_path(args: argparse.Namespace, output_path: Path,
                          out_dir: Path, city: str, state: str,
                          multi_target: bool) -> Path:
    if args.mode == "metro_comparison":
        return output_path if output_path.suffix == ".md" else out_dir / "metro_comparison.md"
    if multi_target:
        safe_city = city.lower().replace(" ", "_").replace(",", "")
        return out_dir / f"{state}_{safe_city}.md"
    return output_path


def run() -> int:
    args = parse_args()
    city_lines = load_lines(Path(args.cities))
    if not city_lines:
        logger.error("No targets to process.")
        return 1

    fit_keys = _parse_fit_keys(args.fit_strategy)
    if fit_keys:
        logger.info(f"Report filter active: only areas matching strategy "
                    f"{sorted(fit_keys)} will be shown in Markdown/HTML.")

    # Cache wiring — one shared instance per pipeline run.
    cache_mode = "no-cache" if not CACHE_ENABLED else args.cache_mode
    cache = Cache(CACHE_DB, mode=cache_mode)
    logger.info(
        f"Cache: mode={cache_mode}, db={CACHE_DB} "
        f"(CACHE_ENABLED={CACHE_ENABLED})"
    )
    _census.set_cache(cache)
    _bls_or_labor.set_cache(cache)
    _job_postings.set_cache(cache)

    client = GooglePlacesClient(cache=cache)
    enrollware_records: List[Any] = []
    if not args.no_enrollware:
        enroll_path = Path(args.enrollware_file) if args.enrollware_file else None
        locations_path = (
            Path(args.enrollware_locations_file)
            if args.enrollware_locations_file else None
        )
        enrollware_records = _enrollware.load_records(
            enroll_path, locations_path=locations_path
        )
        if enrollware_records:
            logger.info(
                f"Enrollware history loaded before scoring: "
                f"{len(enrollware_records)} class record(s)."
            )
    zip_demand_by_zip = (
        _zip_demand.aggregate_zip_demand(enrollware_records)
        if enrollware_records else {}
    )
    zip_centroids = _zip_demand.load_zip_centroids() if zip_demand_by_zip else {}
    if zip_demand_by_zip:
        # Surface the otherwise-silent radius-matching gap: demand ZIPs with no
        # centroid are invisible to exact_plus_radius resolution.
        _coverage = _zip_demand.audit_centroid_coverage(
            zip_demand_by_zip, zip_centroids)
        if not zip_centroids:
            logger.info(
                f"zip_demand: {_coverage.summary()}; exact-ZIP and city "
                f"fallback still active."
            )
        elif _coverage.missing_zips:
            logger.warning(
                f"zip_demand: {_coverage.summary()}. Run "
                f"scripts/build_zip_centroids.py to refresh "
                f"{_zip_demand.ZIP_CENTROIDS_FILE.name}."
            )
    zip_reference_avg = (
        _zip_demand.overall_reference_avg(enrollware_records)
        if enrollware_records else None
    )
    zip_export_latest = (
        _zip_demand.latest_export_date(enrollware_records)
        if enrollware_records else None
    )
    all_ranked: List[Tuple[Dict, Dict]] = []
    per_area_ranked: List[Tuple[str, str, List[Tuple[Dict, Dict]]]] = []
    # Markdown is rendered after the AI summary is built (so it can include it),
    # so we stash the render jobs here and flush them near the end.
    pending_markdown: List[Dict[str, Any]] = []
    multi_target = len(city_lines) > 1
    output_path = Path(args.output)
    dedupe_distance = (
        METRO_DEDUPE_DISTANCE_MILES
        if args.dedupe_distance_miles is None and args.mode == "metro_comparison"
        else (args.dedupe_distance_miles or 0.0)
    )

    if args.mode == "metro_comparison":
        out_dir = output_path.parent if output_path.suffix else output_path
    elif multi_target and output_path.suffix == ".md":
        out_dir = output_path.with_suffix("")
    else:
        out_dir = output_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    for line in city_lines:
        city, state = parse_target_line(line, args.state)
        if not state:
            logger.warning(f"Skipping {city!r}: no state provided.")
            continue
        center = geocode_city(city, state)
        if center is None:
            logger.warning(f"Could not geocode {city}, {state}; skipping.")
            continue
        logger.info(f"=== {city}, {state} @ {center.lat:.4f},{center.lon:.4f} ===")

        # Dense-metro auto-detection: one CPR competitor probe at the
        # configured radius. If 20+ providers exist, the configured area
        # is dense enough that the original radius will collapse every
        # candidate into the same scores — auto-reduce radius and grid.
        effective_radius_miles = args.radius_miles
        effective_grid_spacing = args.grid_spacing_miles
        density_probe = None
        if (not args.no_dense_mode
                and args.mode != "metro_comparison"):
            density_probe = probe_density(
                client,
                center=center.as_tuple(),
                radius_miles=args.radius_miles,
                grid_spacing_miles=args.grid_spacing_miles,
                threshold=args.dense_threshold,
            )
            logger.info(f"{city}: density probe — {density_probe.reason}")
            if density_probe.is_dense:
                effective_radius_miles = density_probe.recommended_radius_miles
                effective_grid_spacing = density_probe.recommended_grid_spacing_miles

        candidates = _candidate_points_for_mode(
            args.mode,
            center.as_tuple(),
            effective_radius_miles,
            effective_grid_spacing,
            args.max_candidates,
        )
        logger.info(f"{city}: {len(candidates)} candidate point(s).")

        # OSM commercial-zone filtering: drop grid points that fall in
        # residential / industrial blocks. Only applied in grid modes —
        # metro_comparison uses a single coarse anchor per area.
        if (not args.no_osm_zoning
                and args.mode != "metro_comparison"
                and _osm_zoning.is_available()
                and len(candidates) > 1):
            bbox = bbox_for_radius(center.as_tuple(),
                                   effective_radius_miles * 1.5)
            polygons = _osm_zoning.fetch_commercial_polygons(bbox, cache=cache)
            if polygons:
                kept, dropped = filter_points_to_commercial(
                    [tuple(c) for c in candidates],
                    polygons,
                    max_distance_meters=args.osm_zone_buffer_meters,
                    min_keep=3,
                )
                if dropped:
                    logger.info(
                        f"{city}: OSM zoning kept {len(kept)} of "
                        f"{len(candidates)} grid point(s); dropped "
                        f"{len(dropped)} as non-commercial."
                    )
                candidates = kept
            else:
                logger.info(
                    f"{city}: OSM returned 0 commercial polygons "
                    f"— skipping zoning filter."
                )

        ranked: List[Tuple[Dict, Dict]] = []
        for idx, (lat, lon) in enumerate(candidates):
            try:
                profile = build_area_profile(
                    client, city, state, lat, lon, effective_radius_miles,
                    candidate_index=idx,
                    candidate_name=(
                        f"{city} comparison area"
                        if args.mode == "metro_comparison"
                        else f"{city} grid #{idx}"
                    ),
                    candidate_source=args.mode,
                    analyze_competitor_websites=args.analyze_competitor_websites,
                    analyze_reviews=args.analyze_reviews,
                )
            except Exception as exc:
                logger.warning(f"profile failed for ({lat:.4f},{lon:.4f}): {exc}")
                continue
            profile["mode"] = args.mode
            profile["comparison_area"] = city
            if density_probe is not None:
                profile["density_probe"] = {
                    "competitor_count": density_probe.competitor_count,
                    "configured_radius_miles": density_probe.radius_miles,
                    "is_dense": density_probe.is_dense,
                    "effective_radius_miles": effective_radius_miles,
                    "effective_grid_spacing_miles": effective_grid_spacing,
                    "reason": density_probe.reason,
                }
            if enrollware_records:
                profile["historical_performance"] = (
                    build_candidate_historical_performance(
                        enrollware_records,
                        city=city,
                        state=state,
                    )
                )
            if zip_demand_by_zip:
                anchor_block = profile.get("anchor") or {}
                profile["zip_demand"] = _zip_demand.build_candidate_zip_demand(
                    zip_demand_by_zip,
                    candidate_zip=_zip_demand.parse_zip(
                        anchor_block.get("formatted_address")),
                    latitude=profile.get("latitude"),
                    longitude=profile.get("longitude"),
                    city=city,
                    centroids=zip_centroids,
                    reference_avg=zip_reference_avg,
                    latest_export_date=zip_export_latest,
                )

            # Drive-time catchment: when an isochrone provider is configured,
            # replace the circular catchment with a polygon by post-filtering
            # demand + competitor records to the polygon interior. ORS is
            # tried first (card-free), then Mapbox.
            if args.catchment_minutes > 0:
                iso_provider = None
                if _ors.is_configured():
                    iso_provider = _ors
                elif _mapbox.is_configured():
                    iso_provider = _mapbox
                if iso_provider is not None:
                    polygon = iso_provider.fetch_isochrone(
                        origin=(lat, lon),
                        minutes=args.catchment_minutes,
                        profile=args.catchment_profile,
                        cache=cache,
                    )
                    if polygon:
                        apply_catchment_filter(profile, polygon)
                    else:
                        logger.info(
                            f"{city}: isochrone unavailable for "
                            f"({lat:.4f},{lon:.4f}); using circular radius."
                        )

            scored = score_profile(profile)
            ranked.append((profile, scored))

            if args.save_profiles:
                pfile = ENRICHED_DIR / f"{profile['candidate_id']}.json"
                pfile.write_text(
                    json.dumps(
                        {"profile": _strip_internal(profile), "scored": scored},
                        default=str, indent=2,
                    ),
                    encoding="utf-8",
                )

        ranked.sort(key=lambda ps: _site_score(ps[1]), reverse=True)
        if dedupe_distance > 0 and args.mode != "metro_comparison":
            ranked = deduplicate_ranked_candidates(ranked, dedupe_distance)
        if not args.keep_non_viable:
            ranked = _filter_viable(ranked)
        if not args.no_cohort_normalization:
            apply_cohort_normalization(ranked, blend=args.cohort_blend)
            ranked.sort(key=lambda ps: _site_score(ps[1]), reverse=True)
        apply_cohort_confidence(ranked)
        apply_rent_estimates(ranked)
        _assign_candidate_ranks(ranked)
        _assign_score_deltas(ranked)
        per_area_ranked.append((city, state, ranked))

        if args.mode != "metro_comparison":
            all_ranked.extend(ranked)
            report_ranked = _filter_fit(ranked, fit_keys)
            if fit_keys and not report_ranked:
                logger.warning(f"{city}: no areas matched --fit-strategy "
                               f"{sorted(fit_keys)}; Markdown report is empty.")
            # Defer rendering until the AI summary (built once, below) exists,
            # so the Markdown can embed the same narrative as the HTML.
            pending_markdown.append({
                "kind": "single",
                "city": city,
                "state": state,
                "ranked": report_ranked,
                "path": _markdown_output_path(
                    args, output_path, out_dir, city, state, multi_target),
            })

    city_rankings: List[Dict[str, Any]] = []
    if args.mode == "metro_comparison":
        for _, _, ranked in per_area_ranked:
            all_ranked.extend(ranked)
        all_ranked.sort(key=lambda ps: _site_score(ps[1]), reverse=True)
        if dedupe_distance > 0:
            all_ranked = deduplicate_ranked_candidates(all_ranked, dedupe_distance)
        if not args.keep_non_viable:
            all_ranked = _filter_viable(all_ranked)
        if not args.no_cohort_normalization:
            apply_cohort_normalization(all_ranked, blend=args.cohort_blend)
            all_ranked.sort(key=lambda ps: _site_score(ps[1]), reverse=True)
        apply_cohort_confidence(all_ranked)
        apply_rent_estimates(all_ranked)
        _assign_candidate_ranks(all_ranked)
        _assign_score_deltas(all_ranked)
        city_rankings = _build_city_rankings(all_ranked)
        report_ranked = _filter_fit(all_ranked, fit_keys)
        if fit_keys and not report_ranked:
            logger.warning(f"No areas matched --fit-strategy {sorted(fit_keys)}; "
                           f"Markdown/HTML reports are empty.")
        fit_areas = {
            str(p.get("comparison_area") or p.get("city"))
            for p, _ in report_ranked
        }
        report_city_rankings = (
            [r for r in city_rankings if str(r.get("area")) in fit_areas]
            if fit_keys else city_rankings
        )
        pending_markdown.append({
            "kind": "metro",
            "ranked": report_ranked,
            "city_rankings": report_city_rankings,
            "path": _markdown_output_path(
                args, output_path, out_dir,
                "metro_comparison", args.state.upper(), False,
            ),
        })
    else:
        all_ranked.sort(key=lambda ps: _site_score(ps[1]), reverse=True)

    all_rows: List[Dict[str, Any]] = [
        candidate_to_row(profile, scored) for profile, scored in all_ranked
    ]
    write_csv_report(all_rows, Path(args.csv_output))

    context = {
        "mode": args.mode,
        "report_style": args.report_style,
        "fit_strategy": sorted(fit_keys),
        "cache_session_as_of": cache.snapshot_session(),
        "radius_miles": args.radius_miles,
        "grid_spacing_miles": args.grid_spacing_miles,
        "max_candidates": args.max_candidates,
        "dedupe_distance_miles": dedupe_distance,
        "cities": city_lines,
        "default_state": args.state,
        "city_rankings": city_rankings,
    }

    # Report-wide ZIP demand dataset for the HTML visualization layer (table +
    # scatter/regression + centroid map). Built once from the same aggregate the
    # per-candidate payloads use, so the report and the cards never disagree.
    if zip_demand_by_zip:
        context["zip_demand_report"] = _zip_demand.build_zip_demand_report(
            zip_demand_by_zip,
            centroids=zip_centroids,
            reference_avg=zip_reference_avg,
            latest_export_date=zip_export_latest,
        )

    # Phase 4B — course-performance evaluation from ALLCPR's Enrollware history.
    # Orthogonal to the per-candidate site scoring: it answers "which course
    # type performs best in this area" from real class records, not locations.
    if not args.no_enrollware:
        if enrollware_records:
            # Single-target runs filter to that city; multi-target stays
            # ALLCPR-wide (one course read for the whole report). Course
            # performance is orthogonal to site geocoding, so when exactly one
            # target was requested we filter to it even if its candidate
            # geocoding failed (e.g. Maps billing off) — the Enrollware history
            # still knows that city.
            cp_city, cp_state = (None, None)
            if len(per_area_ranked) == 1:
                cp_city, cp_state = per_area_ranked[0][0], per_area_ranked[0][1]
            elif not per_area_ranked and len(city_lines) == 1:
                cp_city, cp_state = parse_target_line(city_lines[0], args.state)
            # Area-level public-demand / competition signals for the Phase 5
            # evaluation graph: read the recommended (rank-1) candidate's
            # sub-scores. Missing keys stay None (the graph renormalizes).
            eval_demand = eval_competition = None
            if all_ranked:
                _, top_scored = all_ranked[0]
                _sub = (top_scored or {}).get("sub_scores") or {}
                _job = (top_scored or {}).get("job_demand") or {}
                eval_demand = {
                    "demand_score": _sub.get("demand_score"),
                    "healthcare_training_ecosystem_score":
                        _sub.get("healthcare_training_ecosystem_score"),
                    "job_certification_demand_score":
                        _job.get("job_certification_demand_score"),
                }
                eval_competition = {
                    "competition_gap_score": _sub.get("competition_gap_score"),
                }
            course_perf = build_course_performance_section(
                enrollware_records,
                city=cp_city,
                state=cp_state,
                demand_counts=aggregate_demand_counts(all_ranked),
                demand=eval_demand,
                competition=eval_competition,
            )
            if course_perf:
                context["course_performance"] = course_perf
                logger.info(
                    f"Course performance: {course_perf['total_classes']} class "
                    f"record(s) across {len(course_perf['course_types'])} "
                    f"course type(s)."
                )

    # Optional AI executive summary (OpenAI / Groq). Rephrases the
    # deterministic interpretation; adds no figures. No-ops without a key.
    if not args.no_ai_summary and _ai_summary.is_configured():
        summary = _ai_summary.generate_executive_summary({
            "context": context,
            "report_interpretation": build_report_interpretation(all_ranked),
        })
        if summary:
            context["ai_summary"] = summary
            logger.info(
                f"AI summary attached via {summary['provider']}/{summary['model']}."
            )

    # The AI summary (if any) now exists — render + write the deferred Markdown
    # reports so they embed the same narrative the HTML/JSON carry.
    ai = context.get("ai_summary")
    for job in pending_markdown:
        if job["kind"] == "metro":
            md = render_metro_comparison_report(
                state=args.state.upper(),
                radius_miles=args.radius_miles,
                ranked=job["ranked"],
                city_rankings=job["city_rankings"],
                report_style=args.report_style,
                cache_session_as_of=cache.snapshot_session(),
                ai_summary=ai,
            )
        else:
            md = render_markdown_report(
                job["city"], job["state"], args.radius_miles,
                job["ranked"],
                report_style=args.report_style,
                cache_session_as_of=cache.snapshot_session(),
                ai_summary=ai,
                course_performance=context.get("course_performance"),
            )
        write_markdown_report(md, job["path"])

    # JSON file keeps every evaluated area; the HTML report honors the filter.
    write_json_report(
        ranked=all_ranked,
        context=context,
        path=Path(args.json_output),
    )
    if args.html_output:
        html_ranked = _filter_fit(all_ranked, fit_keys)
        html_payload = render_json(html_ranked, context=context)
        write_html_report(html_payload,
                          Path(args.html_output),
                          report_style=args.report_style)
        # Export the dashboard JSON from the same report payload (web_app.py).
        write_latest_report_json(html_payload, output_path=args.dashboard_json)
    if args.purge_stale_cache:
        removed = cache.purge_stale()
        logger.info(f"Cache: purged {removed} stale entries")
    logger.info("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
