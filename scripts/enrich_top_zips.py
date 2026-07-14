"""
Phase-2 enrichment for selected/top modeled ZIPs (OFFLINE batch).

Reads the national modeled layer, selects the strongest ZIPs for a chosen
course, and enriches just those with finalist-context signals — Google Places
facility/competitor context, Routes drive-time, and the manual commercial-
validation CSV — then writes ``national_demand_enriched.json``.

Why offline: enriching every US ZIP live would be slow and expensive. We enrich
a small top-N set in a controlled batch and save the result; the website only
ever reads the saved JSON. Places/Routes are pluggable stubs by default (return
``{}`` and make no live calls) so this runs safely with no API keys; commercial
validation works today from the CSV.

Strategy after the Places backtest: live Places moved overall correlation only
0.103 → 0.105 and saturated dense ZIPs at the ~20-result nearby-search cap, so
Places is NOT the default national scoring engine. Treat Places as context /
validation / display enrichment for selected finalists. Any Places-driven score
change must remain an explicit, backtest-gated experiment; keep bulk public
datasets as the preferred path for national scoring improvements.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import (
    CACHE_DB,
    CACHE_ENABLED,
    COMPETITION_SATURATION_COUNT,
    ENHANCED_SIGNAL_RADIUS_MILES,
    GOOGLE_MAPS_API_KEY,
    PROCESSED_DIR,
    ttl_for,
    ZIP_MODEL_BOUNDS,
    ZIP_MODELED_WEIGHTS_BLS,
)
from app.reports.commercial_validation import (
    COMMERCIAL_VALIDATION_FILE,
    load_commercial_summaries,
)
from app.collectors import census_bulk
from app.collectors.google_places import GooglePlacesError
from app.reports.report_export import NATIONAL_DEMAND_PATH
from app.scoring.api_candidate_filter import filter_api_candidates
from app.scoring.enhanced_opportunity_signals import (
    _dedupe,
    compute_enhanced_signals,
)
from app.scoring.zip_modeled_opportunity import (
    _rural_market_caps,
    automated_validation,
    compute_zip_modeled_opportunity,
    signal_weight_breakdown,
)
from app.utils.cache import Cache, build_cache_key
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

NATIONAL_DEMAND_ENRICHED_FILE = PROCESSED_DIR / "national_demand_enriched.json"
ENRICHMENT_CACHE_SUMMARY_FILE = PROCESSED_DIR / "enrichment_cache.json"
RESUME_ENRICHMENT_FIELDS = {
    "hospital_count",
    "urgent_care_count",
    "nursing_school_count",
    "healthcare_facility_count",
    "healthcare_facility_density",
    "healthcare_poi_count",
    "medical_office_count",
    "ems_fire_count",
    "healthcare_provider_count",
    "nurse_count",
    "physician_count",
    "clinic_provider_count",
    "provider_density_per_10k_pop",
    "college_count",
    "health_program_school_count",
    "student_enrollment_count",
    "childcare_count",
    "school_count",
    "training_school_count",
    "community_facility_count",
    "community_facility_density",
    "training_school_density",
    "enhanced_signal_debug",
    "parking_proxy_score",
    "commercial_access_proxy_score",
    "competitor_count",
    "competitor_density",
    "avg_competitor_rating",
    "competitor_schedule_count",
    "competition_gap_score",
    "drive_time_access_score",
    "parking_score",
    "commercial_space_available",
    "commercial_ready",
    "classroom_fit",
    "estimated_rent",
    "rent_source",
    "commercial",
    "enrichment_tier",
    "enrichment_sources",
    "enrichment_updated_at",
}

# Course → modeled field used for ranking/selection.
COURSE_FIELD = {"overall": "overall", "aha_bls": "bls_demand",
                "arc_bls": "bls_demand", "arc_cpr": "cpr_demand"}

# Google Places enrichment (only when --use-places). Queries are grouped by the
# enhanced modeled signal they feed; each tuple `(tag, search kwargs)` is one
# cached nearby/text search. Results are deduped (by place_id) within a group so
# overlapping queries never double-count, then normalized per ZIP square mile by
# app.scoring.enhanced_opportunity_signals. Broadening these categories is the
# reproducible enrichment step that populates healthcare_facility_density,
# training_school_density, and competition_gap_score.
PLACES_RADIUS_MILES = ENHANCED_SIGNAL_RADIUS_MILES

# healthcare_facility_density: hospitals, urgent care, clinics, medical centers,
# doctor offices, nursing facilities.
HEALTHCARE_PLACES_QUERIES = [
    ("hospital",          {"place_type": "hospital"}),
    ("urgent_care",       {"keyword": "urgent care"}),
    ("clinic",            {"keyword": "medical clinic"}),
    ("doctor_office",     {"keyword": "doctor office"}),
    ("nursing_facility",  {"keyword": "nursing home"}),
]
# training_school_density: CPR/BLS training, nursing schools, EMT schools,
# medical-assistant / vocational healthcare programs.
TRAINING_PLACES_QUERIES = [
    ("cpr_training",      {"keyword": "CPR training class"}),
    ("nursing_school",    {"keyword": "nursing school"}),
    ("emt_school",        {"keyword": "EMT training school"}),
    ("medical_assistant", {"keyword": "medical assistant program"}),
]
# competition_gap_score: direct CPR/BLS/first-aid certification competitors.
COMPETITOR_PLACES_QUERIES = [
    ("cpr_bls_cert",      {"keyword": "CPR BLS certification class"}),
    ("first_aid_cert",    {"keyword": "first aid certification class"}),
]
ALL_PLACES_QUERIES = (
    HEALTHCARE_PLACES_QUERIES + TRAINING_PLACES_QUERIES + COMPETITOR_PLACES_QUERIES
)
PLACES_CALLS_PER_ZIP = len(ALL_PLACES_QUERIES)
ESTIMATED_ROUTES_CALLS_PER_ZIP = 1   # if Routes were wired (no key → stub)
# competitors at/above this → competition_gap_score 0 (env-overridable policy).
COMPETITOR_GAP_CAP = COMPETITION_SATURATION_COUNT
LIVE_PRIORITY_MINIMUM = {"medium", "high", "finalist"}
ENRICHMENT_SUMMARY = "Enriched validation available for priority ZIPs."
ENRICHMENT_SCOPE_NOTE = (
    "Enrichment covers priority ZIPs only. Baseline rows remain valid public-data "
    "estimates and can be force-enriched later with --zips or --zips-from when "
    "requested by state, customer, or search."
)


def _is_abortable_places_error(message: str) -> bool:
    """True for global Places failures (quota 429 / auth-billing 403) that mean
    the whole run should stop rather than retry the same error on every ZIP."""
    msg = (message or "").upper()
    return any(t in msg for t in (
        "429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE_LIMIT", "OVER_QUERY_LIMIT",
        "403", "PERMISSION_DENIED", "REQUEST_DENIED", "BILLING", "API_KEY", "401",
    ))


def load_zips_from(path: Path) -> List[str]:
    """Load ZIP codes from a selector JSON payload, list, or simple row file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        raw_rows = payload
    else:
        raw_rows = payload.get("rows") or payload.get("zips") or []
    zips: List[str] = []
    seen = set()
    for item in raw_rows:
        if isinstance(item, dict):
            zip_code = item.get("zip") or item.get("zipcode") or item.get("zcta")
        else:
            zip_code = item
        zip_code = str(zip_code or "").strip()
        if zip_code and zip_code not in seen:
            seen.add(zip_code)
            zips.append(zip_code)
    return zips


def _parse_cache_as_of(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fresh_within(as_of: str | None, refresh_days: int) -> bool:
    parsed = _parse_cache_as_of(as_of)
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed <= timedelta(days=refresh_days)


class InstrumentedPlacesClient:
    """Google Places wrapper that honors refresh-days and reports cache usage."""

    def __init__(
        self,
        live_client: Any,
        *,
        cache: Optional[Cache],
        refresh_days: int,
        force_refresh: bool = False,
    ) -> None:
        self.live_client = live_client
        self.cache = cache
        self.refresh_days = max(0, int(refresh_days))
        self.force_refresh = force_refresh
        self.live_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_stale = 0
        self.current_zip: Optional[str] = None
        self.zip_stats: Dict[str, Dict[str, int]] = {}

    def begin_zip(self, zip_code: str) -> None:
        self.current_zip = str(zip_code)
        self.zip_stats.setdefault(self.current_zip, {
            "live_calls": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_stale": 0,
        })

    def _bump(self, field: str) -> None:
        if field == "live_calls":
            self.live_calls += 1
        elif field == "cache_hits":
            self.cache_hits += 1
        elif field == "cache_misses":
            self.cache_misses += 1
        elif field == "cache_stale":
            self.cache_stale += 1
        if self.current_zip:
            self.zip_stats.setdefault(self.current_zip, {
                "live_calls": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "cache_stale": 0,
            })
            self.zip_stats[self.current_zip][field] += 1

    def nearby_search(
        self,
        location,
        radius_meters,
        place_type=None,
        keyword=None,
        max_pages=2,
    ) -> List[Dict]:
        params = {
            "latitude": location[0],
            "longitude": location[1],
            "radius_meters": radius_meters,
            "place_type": place_type or "",
            "keyword": keyword or "",
            "max_pages": max_pages,
        }
        key = build_cache_key("google_places", "nearby_search", params)
        if self.cache is not None and not self.force_refresh:
            entry = self.cache.info(key)
            if entry is not None and _fresh_within(entry.as_of, self.refresh_days):
                self._bump("cache_hits")
                return entry.value
            if entry is not None:
                self._bump("cache_stale")
            else:
                self._bump("cache_misses")
        elif self.cache is not None and self.force_refresh:
            # Count an existing entry as intentionally bypassed/stale for the
            # run summary; this keeps forced refreshes auditable.
            if self.cache.info(key) is not None:
                self._bump("cache_stale")
            else:
                self._bump("cache_misses")

        value = self.live_client._nearby_search_live(
            location,
            radius_meters,
            place_type=place_type,
            keyword=keyword,
            max_pages=max_pages,
        )
        self._bump("live_calls")
        if self.cache is not None:
            self.cache.set(
                key,
                value,
                ttl_seconds=ttl_for("google_places", "nearby_search"),
                provider="google_places",
            )
        return value

    def summary(self) -> Dict[str, Any]:
        cache_only = sum(
            1
            for stats in self.zip_stats.values()
            if stats.get("live_calls", 0) == 0 and stats.get("cache_hits", 0) > 0
        )
        return {
            "live_places_calls": self.live_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_stale": self.cache_stale,
            "fresh_cached_zip_skips": cache_only,
            "zip_stats": self.zip_stats,
            "refresh_days": self.refresh_days,
            "force_refresh": self.force_refresh,
        }


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def select_top_zips(
    rows: List[Dict[str, Any]],
    *,
    course: str = "overall",
    top_n: int = 100,
    state: Optional[str] = None,
    min_score: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Top-N ZIP rows by the selected course score, after optional filters."""
    field = COURSE_FIELD.get(course, "overall")
    pool = list(rows)
    if state:
        with_state = [r for r in pool if r.get("state")]
        if with_state:
            pool = [r for r in with_state
                    if str(r.get("state")).upper() == state.upper()]
        else:
            logger.warning("No `state` field in national rows — "
                           "skipping state filter.")
    if min_score is not None:
        pool = [r for r in pool if (r.get(field) or 0) >= min_score]
    pool.sort(key=lambda r: (r.get(field) or 0), reverse=True)
    return pool[:max(0, top_n)]


def _cand_num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


# Drop ZIPs that aren't worth a paid enrichment call. Returns a reason string
# (None = keep). Mirrors the filters requested for the national expansion run.
LOW_CONFIDENCE = {"missing", "low", "very_low", "very low", "poor", "none"}


def national_candidate_reason(
    row: Dict[str, Any],
    *,
    min_score: Optional[float],
    min_population: Optional[float],
    min_density: float = 300.0,
    high_demand_override: float = 70.0,
) -> Optional[str]:
    lat, lng = _cand_num(row.get("lat")), _cand_num(row.get("lng"))
    if lat is None or lng is None:
        return "missing_coordinates"
    overall = _cand_num(row.get("overall")) or 0.0
    if min_score is not None and overall < min_score:
        return "below_min_score"
    pop = _cand_num(row.get("population")) or 0.0
    if min_population is not None and pop < min_population:
        return "below_min_population"
    if str(row.get("data_confidence") or "").strip().lower() in LOW_CONFIDENCE:
        return "missing_acs_fields"
    density = _cand_num(row.get("population_density")) or 0.0
    # Keep very-low-density ZIPs only when modeled demand is unusually high.
    if density < min_density and overall < high_demand_override:
        return "low_density"
    return None


def filter_national_candidates(
    rows: List[Dict[str, Any]],
    *,
    min_score: Optional[float],
    min_population: Optional[float],
) -> tuple:
    """Return (kept_rows, drop_reason_counter) per the national candidate gate."""
    from collections import Counter
    kept: List[Dict[str, Any]] = []
    dropped: "Counter[str]" = Counter()
    for row in rows:
        reason = national_candidate_reason(
            row, min_score=min_score, min_population=min_population)
        if reason is None:
            kept.append(row)
        else:
            dropped[reason] += 1
    return kept, dict(dropped)


# --------------------------------------------------------------------------- #
# Pluggable enrichment sources (Places/Routes are stubs by default)
# --------------------------------------------------------------------------- #
def _zip_land_sqmi(zip_row: Dict[str, Any]) -> Optional[float]:
    """ZIP land area (sq mi) for density normalization, never invented.

    Prefers the Census Gazetteer ``land_sqmi`` carried on the row; falls back to
    ``population / population_density`` (which is how density was derived from the
    same Gazetteer area) when the explicit area is absent. Returns ``None`` when
    neither is available so the density signals drop out and renormalize.
    """
    area = _cand_num(zip_row.get("land_sqmi"))
    if area is not None and area > 0:
        return area
    pop = _cand_num(zip_row.get("population"))
    density = _cand_num(zip_row.get("population_density"))
    if pop is not None and density and density > 0:
        return pop / density
    return None


def _enhanced_signal_debug(enhanced: Dict[str, Any]) -> Dict[str, Any]:
    """Combine the enrichment-side debug (raw counts, ZIP area, density) with the
    scoring-side breakdown (normalized, weight, contribution) into one table.

    Weights/contributions are shown for the BLS tilt, the tilt where all three
    enhanced signals carry weight; the same normalized values apply to any tilt.
    """
    feature_keys = (
        "healthcare_facility_density",
        "training_school_density",
        "competition_gap_score",
    )
    signals = {k: enhanced.get(k) for k in feature_keys}
    breakdown = signal_weight_breakdown(signals, ZIP_MODELED_WEIGHTS_BLS)
    by_field = {r["field"]: r for r in breakdown["rows"]}
    dbg = enhanced.get("debug", {})
    rows: List[Dict[str, Any]] = []
    for field in feature_keys:
        field_dbg = dbg.get(field, {})
        b = by_field.get(field, {})
        rows.append({
            "field": field,
            "raw_count": field_dbg.get("raw_count",
                                       field_dbg.get("competitor_count")),
            "land_sqmi": dbg.get("land_sqmi"),
            "value": signals.get(field),        # the modeled feature (density / gap)
            "normalized": b.get("normalized"),
            "weight": b.get("weight"),
            "contribution": b.get("contribution"),
        })
    return {
        "land_sqmi": dbg.get("land_sqmi"),
        "saturation_count": dbg.get("saturation_count"),
        "signals": rows,
    }


def _log_enhanced_signal_debug(zip_code: Any, debug: Dict[str, Any]) -> None:
    for row in debug.get("signals", []):
        logger.debug(
            "[enhanced] zip=%s %s: raw_count=%s land_sqmi=%s value=%s "
            "normalized=%s weight=%s contribution=%s",
            zip_code, row["field"], row["raw_count"], row["land_sqmi"],
            row["value"], row["normalized"], row["weight"], row["contribution"],
        )


def enrich_zip_with_places(zip_row: Dict[str, Any],
                           client: Any = None) -> Dict[str, Any]:
    """Facility/competitor evidence + enhanced modeled signals via Google Places.

    Issues the grouped :data:`HEALTHCARE_PLACES_QUERIES` /
    :data:`TRAINING_PLACES_QUERIES` / :data:`COMPETITOR_PLACES_QUERIES` (cached;
    free on re-run), dedupes each group by place_id, then normalizes the counts
    per ZIP square mile (Gazetteer land area) into ``healthcare_facility_density``
    and ``training_school_density`` and converts the competitor count into a
    configurable 0..100 ``competition_gap_score``. Legacy descriptive counts
    (hospital/urgent-care/nursing-school) are kept for the validation layer and
    display. ``client=None`` (default / no --use-places) → ``{}``: no calls.
    """
    if client is None:
        return {}
    lat, lng = zip_row.get("lat"), zip_row.get("lng")
    if lat is None or lng is None:
        return {}
    radius = int(PLACES_RADIUS_MILES * 1609.34)

    def _run(group: List) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for tag, kwargs in group:
            out[tag] = client.nearby_search(
                (lat, lng), radius, max_pages=1, **kwargs) or []
        return out

    healthcare = _run(HEALTHCARE_PLACES_QUERIES)
    training = _run(TRAINING_PLACES_QUERIES)
    competition = _run(COMPETITOR_PLACES_QUERIES)

    # Deduped counts per category (trusting the targeted queries); dedupe by
    # place_id so a POI returned by two queries in a group is counted once.
    healthcare_pois = _dedupe([p for r in healthcare.values() for p in r])
    training_pois = _dedupe([p for r in training.values() for p in r])
    competitor_pois = _dedupe([p for r in competition.values() for p in r])
    comp_ratings = [p.get("rating") for p in competitor_pois
                    if isinstance(p, dict) and isinstance(p.get("rating"),
                                                          (int, float))]

    enhanced = compute_enhanced_signals(
        land_sqmi=_zip_land_sqmi(zip_row),
        healthcare_count=len(healthcare_pois),
        training_count=len(training_pois),
        competitor_count=len(competitor_pois),
        saturation_count=COMPETITOR_GAP_CAP,
    )
    signal_debug = _enhanced_signal_debug(enhanced)
    _log_enhanced_signal_debug(zip_row.get("zip"), signal_debug)

    return {
        # Legacy descriptive counts (validation layer + display).
        "hospital_count": len(healthcare.get("hospital", [])),
        "urgent_care_count": len(healthcare.get("urgent_care", [])),
        "nursing_school_count": len(training.get("nursing_school", [])),
        "competitor_count": len(competitor_pois),
        "healthcare_facility_count": len(healthcare_pois),
        "training_school_count": len(training_pois),
        "avg_competitor_rating": (round(sum(comp_ratings) / len(comp_ratings), 2)
                                  if comp_ratings else None),
        # Enhanced modeled signals (per-sq-mile densities + configurable gap),
        # fed back into the modeled score via ZIP_MODEL_BOUNDS.
        "healthcare_facility_density": enhanced["healthcare_facility_density"],
        "training_school_density": enhanced["training_school_density"],
        "competition_gap_score": enhanced["competition_gap_score"],
        "enhanced_signal_debug": signal_debug,
        "enrichment_sources": ["google_places"],
        "enrichment_tier": "places",
    }


def enrich_zip_with_routes(zip_row: Dict[str, Any], *,
                           api_key: str = GOOGLE_MAPS_API_KEY) -> Dict[str, Any]:
    """Drive-time / accessibility via Google Routes.

    TODO(phase-2): wire app.collectors.openrouteservice_isochrones or Google
    Routes to a ``drive_time_access_score``. Returns ``{}`` for now.
    """
    return {}


def enrich_zip_with_commercial_validation(
    zip_row: Dict[str, Any],
    summaries: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Map the manual commercial-validation CSV into enrichment fields.

    Works today (no API). Empty dict when the ZIP has no validated space.
    """
    summary = summaries.get(str(zip_row.get("zip")))
    if not summary or not summary.get("commercial_validated"):
        return {}
    return {
        "commercial_space_available": bool(summary.get("available_space_count")),
        "estimated_rent": summary.get("rent_avg"),
        "rent_source": "manual_commercial_validation_csv",
        "enrichment_sources": ["commercial_validation_csv"],
    }


def merge_enrichment(zip_row: Dict[str, Any],
                     enrichment: Dict[str, Any]) -> Dict[str, Any]:
    """Fold enrichment fields into a ZIP row (in place) and flip its tier.

    Descriptive/meta fields are attached; ``enrichment_sources`` accumulate.
    The headline demand score is intentionally left unchanged.
    """
    if not enrichment:
        return zip_row
    sources = list(zip_row.get("enrichment_sources") or [])
    for key, value in enrichment.items():
        if value is None:
            continue
        if key == "enrichment_sources":
            sources.extend(value)
        else:
            zip_row[key] = value
    if sources:
        zip_row["enrichment_sources"] = sorted(set(sources))
    zip_row["tier"] = "enriched"
    zip_row.setdefault("enrichment_tier",
                       enrichment.get("enrichment_tier", "commercial"))
    zip_row["enrichment_updated_at"] = datetime.now(timezone.utc).isoformat()
    return zip_row


def rescore_with_enrichment(
    row: Dict[str, Any],
    enrichment: Dict[str, Any],
    features_by_zip: Dict[str, Dict[str, Any]],
) -> None:
    """Recompute the modeled score with enrichment signals folded in.

    Uses the ZIP's full baseline features (from the cached ACS + gazetteer) so
    the result matches the build path exactly, then overlays any enrichment
    value that is also a scoring signal (``ZIP_MODEL_BOUNDS``). This is what
    lets Places enrichment be tested against overall/bls/cpr in a backtest.
    This path is intentionally experimental: do not promote Places scoring by
    default unless a later backtest improves meaningfully. No-op when features
    for the ZIP aren't available.
    """
    feats = features_by_zip.get(str(row.get("zip")))
    if not feats:
        return
    feats = dict(feats)
    for key, value in enrichment.items():
        if key in ZIP_MODEL_BOUNDS and value is not None:
            feats[key] = value
    scored = compute_zip_modeled_opportunity(feats)
    if scored["overall"] is None:
        return
    row["overall"] = scored["overall"]
    row["bls_demand"] = scored["bls_demand"]
    row["cpr_demand"] = scored["cpr_demand"]
    row["recommendation"] = scored["recommendation"]
    row["data_confidence"] = scored["data_quality"]["confidence"]


def write_enriched_payload(payload: Dict[str, Any], output_path: Path,
                           enriched_zips: List[str]) -> Path:
    """Write the full payload (all rows preserved) with enrichment metadata."""
    payload = dict(payload)
    for row in payload.get("rows") or []:
        validation = automated_validation(
            row,
            row.get("overall"),
            str(row.get("data_confidence") or "missing"),
        )
        caps = _rural_market_caps(
            row,
            overall=row.get("overall"),
            bls=row.get("bls_demand"),
            cpr=row.get("cpr_demand"),
            validation=validation,
        )
        row["overall"] = caps["overall"]
        row["bls_demand"] = caps["bls_demand"]
        row["cpr_demand"] = caps["cpr_demand"]
        validation = automated_validation(
            row,
            row.get("overall"),
            str(row.get("data_confidence") or "missing"),
        )
        row.update(validation)
        row["final_cap_applied"] = caps["final_cap_applied"]
        row["cap_reason"] = caps["cap_reason"]
        row["cap_details"] = caps["cap_details"]
        row["validation_override_applied"] = caps["validation_override_applied"]
        row["recommendation"] = validation.get("validation_tier") or row.get(
            "recommendation")
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["enriched_zip_count"] = len(enriched_zips)
    payload["enriched_zips"] = enriched_zips
    payload["enrichment_scope"] = "priority_zips"
    payload["enrichment_summary"] = ENRICHMENT_SUMMARY
    payload["enrichment_scope_note"] = ENRICHMENT_SCOPE_NOTE
    payload["manual_force_enrichment_supported"] = True
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def merge_prior_enrichment(row: Dict[str, Any], prior: Dict[str, Any]) -> bool:
    """Copy only enrichment evidence from an older enriched output row.

    This preserves Places/commercial context while allowing a rebuilt baseline
    (for example, ACS 2024) to keep its fresh demographics and scores.
    """
    was_enriched = bool(prior.get("enrichment_tier") or prior.get("tier") == "enriched")
    if not was_enriched:
        return False
    for key in RESUME_ENRICHMENT_FIELDS:
        if prior.get(key) is not None:
            row[key] = prior[key]
    row["tier"] = "enriched"
    row.setdefault("enrichment_tier", prior.get("enrichment_tier") or "places")
    return True


def write_enrichment_cache_summary(
    output_path: Path,
    *,
    input_path: Path,
    enriched_output_path: Path,
    selected_zips: List[str],
    enriched_zips: List[str],
    estimated_places_calls: int,
    places_summary: Dict[str, Any],
    started_at: datetime,
) -> Path:
    """Write a JSON run ledger for live/cached Places usage."""
    finished_at = datetime.now(timezone.utc)
    runtime_seconds = round((finished_at - started_at).total_seconds(), 1)
    payload = {
        "generated_at": finished_at.isoformat(),
        "started_at": started_at.isoformat(),
        "runtime_seconds": runtime_seconds,
        "runtime_minutes": round(runtime_seconds / 60.0, 2),
        "input": str(input_path),
        "output": str(enriched_output_path),
        "selected_zip_count": len(selected_zips),
        "enriched_zip_count": len(enriched_zips),
        "estimated_places_calls": estimated_places_calls,
        "actual_live_places_calls": places_summary.get("live_places_calls", 0),
        "cache_hits": places_summary.get("cache_hits", 0),
        "cache_misses": places_summary.get("cache_misses", 0),
        "cache_stale": places_summary.get("cache_stale", 0),
        "fresh_cached_zip_skips": places_summary.get("fresh_cached_zip_skips", 0),
        "refresh_days": places_summary.get("refresh_days"),
        "force_refresh": places_summary.get("force_refresh", False),
        "selected_zips": selected_zips,
        "enriched_zips": enriched_zips,
        "zip_stats": places_summary.get("zip_stats", {}),
    }
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_enrichment(
    payload: Dict[str, Any],
    *,
    course: str,
    top_n: int,
    state: Optional[str],
    min_score: Optional[float],
    max_api_calls: Optional[int],
    commercial_path: Path = COMMERCIAL_VALIDATION_FILE,
    zips: Optional[List[str]] = None,
    places_client: Any = None,
    features_by_zip: Optional[Dict[str, Dict[str, Any]]] = None,
    apply_api_filter: bool = True,
    api_min_score: Optional[float] = None,
    run_stats: Optional[Dict[str, Any]] = None,
    min_population: Optional[float] = None,
    already_enriched: Optional[set] = None,
    checkpoint: Optional[Any] = None,
    batch_size: int = 25,
) -> List[str]:
    """Select + enrich ZIPs in place on ``payload['rows']``.

    Returns the enriched ZIP codes. ``zips`` targets an explicit set (for the
    overlap experiment); otherwise top-N by course + manually-validated ZIPs.
    ``places_client`` (when given) runs live Google Places; ``features_by_zip``
    enables recomputing the score from those signals.
    """
    rows = payload.get("rows") or []
    summaries = load_commercial_summaries(commercial_path)

    if zips:
        want = {str(z) for z in zips}
        selected = [r for r in rows if str(r.get("zip")) in want]
    else:
        # National candidate gate (coords, population, density, ACS, score) runs
        # over ALL rows first, then we take the top-N by course so the budget is
        # spent on the strongest survivors.
        pool = rows
        if min_population is not None:
            pool, drops = filter_national_candidates(
                rows, min_score=min_score, min_population=min_population)
            logger.info(f"National candidate gate kept {len(pool)} / {len(rows)} "
                        f"ZIPs. Dropped: {drops}")
            if run_stats is not None:
                run_stats["total_national_zips"] = len(rows)
                run_stats["candidate_zips"] = len(pool)
                run_stats["filtered_out"] = drops
        selected = select_top_zips(pool, course=course, top_n=top_n,
                                   state=state, min_score=min_score)
        # Always include manually commercial-validated ZIPs — already vetted.
        selected_zips = {r["zip"] for r in selected}
        for row in rows:
            if row.get("zip") in summaries and row["zip"] not in selected_zips:
                selected.append(row)
                selected_zips.add(row["zip"])

    # Resume support: skip ZIPs already enriched in a prior (interrupted) run.
    if already_enriched:
        before = len(selected)
        selected = [r for r in selected
                    if str(r.get("zip")) not in already_enriched]
        skipped = before - len(selected)
        if skipped:
            logger.info(f"[resume] Skipping {skipped} already-enriched ZIP(s).")
        if run_stats is not None:
            run_stats["resume_skipped"] = skipped

    use_places = places_client is not None
    if use_places and apply_api_filter:
        before = len(selected)
        filtered = [
            r for r in filter_api_candidates(selected, min_score=api_min_score)
            if r.get("api_priority") in LIVE_PRIORITY_MINIMUM
        ]
        originals = {str(r.get("zip")): r for r in selected}
        selected = [originals[str(r.get("zip"))] for r in filtered
                    if str(r.get("zip")) in originals]
        logger.info(f"API candidate filter kept {len(selected)} / {before} "
                    f"selected ZIP(s) at medium+ priority for live Places "
                    f"enrichment.")
    est_per_zip = (PLACES_CALLS_PER_ZIP if use_places
                   else PLACES_CALLS_PER_ZIP + ESTIMATED_ROUTES_CALLS_PER_ZIP)
    estimated = len(selected) * est_per_zip
    if max_api_calls is not None and estimated > max_api_calls:
        allowed = max_api_calls // max(1, est_per_zip)
        logger.warning(f"Estimated {estimated} API calls exceeds "
                       f"--max-api-calls={max_api_calls}; trimming "
                       f"{len(selected)} → {allowed} ZIPs.")
        selected = selected[:allowed]
        estimated = len(selected) * est_per_zip
    logger.info(f"Selected {len(selected)} ZIPs (course={course}, "
                f"use_places={use_places}). Estimated Google Places calls: "
                f"~{estimated}"
                + ("" if use_places else " (Places stub off → 0 live calls)."))
    if run_stats is not None:
        run_stats["selected_zips"] = [str(r.get("zip")) for r in selected]
        run_stats["estimated_places_calls"] = (
            len(selected) * PLACES_CALLS_PER_ZIP if use_places else 0
        )

    enriched_zips: List[str] = []
    for idx, row in enumerate(selected, start=1):
        if use_places and hasattr(places_client, "begin_zip"):
            places_client.begin_zip(str(row.get("zip")))
        enrichment: Dict[str, Any] = {}
        try:
            enrichment.update(enrich_zip_with_places(row, places_client))
        except GooglePlacesError as exc:
            # Quota (429) and auth/billing (403) failures are global, not
            # per-ZIP: every remaining call will fail identically. Checkpoint
            # what's done (cached calls persist) and stop cleanly so a later
            # --resume run continues where this left off.
            if _is_abortable_places_error(str(exc)):
                if checkpoint is not None:
                    checkpoint(enriched_zips)
                logger.error(
                    "[ABORT] Google Places stopped the run at ZIP %s after %d "
                    "enriched: %s. Checkpoint saved — re-run with --resume once "
                    "quota resets / is raised or billing is restored.",
                    row.get("zip"), len(enriched_zips), exc)
                break
            raise
        enrichment.update(enrich_zip_with_routes(row))
        enrichment.update(enrich_zip_with_commercial_validation(row, summaries))
        if enrichment:
            merge_enrichment(row, enrichment)
            if use_places and features_by_zip:
                rescore_with_enrichment(row, enrichment, features_by_zip)
            enriched_zips.append(row["zip"])
        # Checkpoint every batch so an interruption (or rate-limit) keeps progress.
        if checkpoint is not None and batch_size > 0 and idx % batch_size == 0:
            checkpoint(enriched_zips)
            logger.info(f"[checkpoint] {idx}/{len(selected)} processed, "
                        f"{len(enriched_zips)} enriched so far — saved.")
    if checkpoint is not None:
        checkpoint(enriched_zips)
    logger.info(f"Enriched {len(enriched_zips)} ZIP(s) with available signals.")
    return enriched_zips


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=str(NATIONAL_DEMAND_PATH))
    ap.add_argument("--output", default=str(NATIONAL_DEMAND_ENRICHED_FILE))
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--state", default="")
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--min-population", type=float, default=None,
                    help="National candidate gate: drop ZIPs below this "
                         "population (also drops missing coords/ACS and very "
                         "low-density ZIPs unless demand is high). e.g. 2000.")
    ap.add_argument("--resume", action="store_true",
                    help="Continue a prior run: load the existing --output, skip "
                         "ZIPs already enriched there, and checkpoint progress.")
    ap.add_argument("--batch-size", type=int, default=25,
                    help="Write the enriched output every N processed ZIPs so an "
                         "interruption does not lose progress.")
    ap.add_argument("--course", choices=tuple(COURSE_FIELD), default="overall")
    ap.add_argument("--dry-run", action="store_true",
                    help="Select + log; do not write output.")
    ap.add_argument("--max-api-calls", type=int, default=None,
                    help="Safety cap on estimated live API calls.")
    ap.add_argument("--use-places", action="store_true",
                    help="Run LIVE Google Places (costs money; cached). Without "
                         "this, Places is a no-op stub.")
    ap.add_argument("--zips", default="",
                    help="Comma-separated ZIPs to enrich explicitly (overrides "
                         "top-N selection; used for the overlap experiment).")
    ap.add_argument("--zips-from", default="",
                    help="JSON file from select_api_candidates.py (or a list) "
                         "containing ZIPs to enrich explicitly.")
    ap.add_argument("--api-min-score", type=float, default=None,
                    help="When --use-places is enabled, require this offline "
                         "API-candidate score before live Places calls.")
    ap.add_argument("--disable-api-filter", action="store_true",
                    help="Bypass the offline API-candidate gate for --use-places.")
    ap.add_argument("--refresh-days", type=int, default=30,
                    help="Skip live Places calls when cached nearby_search "
                         "results are fresher than this many days.")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Ignore fresh Places cache entries and call live APIs.")
    ap.add_argument("--rescore-places", action="store_true",
                    help="Experimental: feed Places-derived signals back into "
                         "modeled scoring. Default is context/display only.")
    ap.add_argument("--cache-summary-output",
                    default=str(ENRICHMENT_CACHE_SUMMARY_FILE),
                    help="Write live/cache Places call accounting here.")
    ap.add_argument("--acs-year", type=int, default=census_bulk.DEFAULT_ACS_YEAR,
                    help="ACS vintage for score recompute (must match the build).")
    ap.add_argument("--gazetteer-year", type=int, default=2024)
    args = ap.parse_args(argv)
    started_at = datetime.now(timezone.utc)

    in_path = Path(args.input)
    if not in_path.exists():
        logger.error(f"Input not found: {in_path}. Run build_national_demand.py first.")
        return 1
    payload = json.loads(in_path.read_text(encoding="utf-8"))

    # Resume: build the enriched payload on top of the existing output so prior
    # enrichments are preserved, and skip ZIPs already done there.
    out_path = Path(args.output)
    already_enriched: set = set()
    if args.resume and out_path.exists():
        prior = json.loads(out_path.read_text(encoding="utf-8"))
        prior_by_zip = {str(r.get("zip")): r for r in prior.get("rows") or []}
        for row in payload.get("rows") or []:
            pz = prior_by_zip.get(str(row.get("zip")))
            if pz and merge_prior_enrichment(row, pz):
                already_enriched.add(str(row.get("zip")))
        logger.info(f"[resume] Loaded {len(already_enriched)} already-enriched "
                    f"ZIP(s) from {out_path}.")

    def _checkpoint(enriched_so_far: List[str]) -> None:
        if not args.dry_run:
            write_enriched_payload(payload, out_path,
                                   sorted(already_enriched.union(enriched_so_far)))

    zip_list = [z.strip() for z in args.zips.split(",") if z.strip()]
    if args.zips_from:
        from_file = load_zips_from(Path(args.zips_from))
        logger.info(f"Loaded {len(from_file)} ZIP(s) from {args.zips_from}.")
        zip_list.extend(from_file)
    if zip_list:
        seen = set()
        zip_list = [z for z in zip_list if not (z in seen or seen.add(z))]
    else:
        zip_list = None

    # Live Places is context/display-only by default. Per-ZIP baseline features
    # are loaded only for the explicit --rescore-places backtest experiment.
    places_client = None
    features_by_zip: Dict[str, Dict[str, Any]] = {}
    if args.use_places and args.dry_run:
        logger.info("[dry-run] --use-places ignored; no live Places client is "
                    "created and no paid calls are made.")
    if args.use_places and not args.dry_run:
        from app.collectors.google_places import GooglePlacesClient
        cache = Cache(CACHE_DB) if CACHE_ENABLED else None
        live_client = GooglePlacesClient(cache=None)
        places_client = InstrumentedPlacesClient(
            live_client,
            cache=cache,
            refresh_days=args.refresh_days,
            force_refresh=args.force_refresh,
        )
        if args.rescore_places:
            from scripts.build_national_demand import features_for_zip
            from scripts.build_zip_centroids import (
                fetch_gazetteer_text, gazetteer_url, parse_gazetteer_records)
            acs = census_bulk.fetch_acs_zcta_bulk(args.acs_year, cache=cache)
            gaz = parse_gazetteer_records(
                fetch_gazetteer_text(gazetteer_url(args.gazetteer_year)))
            for z in (zip_list or [r.get("zip") for r in payload.get("rows") or []]):
                if z in acs and z in gaz:
                    features_by_zip[z] = features_for_zip(gaz[z], acs[z])

    run_stats: Dict[str, Any] = {}
    enriched = run_enrichment(
        payload, course=args.course, top_n=args.top_n,
        state=args.state or None, min_score=args.min_score,
        max_api_calls=args.max_api_calls, zips=zip_list,
        places_client=places_client, features_by_zip=features_by_zip,
        apply_api_filter=not args.disable_api_filter,
        api_min_score=args.api_min_score,
        run_stats=run_stats,
        min_population=args.min_population,
        already_enriched=already_enriched,
        checkpoint=(None if args.dry_run else _checkpoint),
        batch_size=args.batch_size)

    all_enriched = sorted(already_enriched.union(enriched))
    if args.dry_run:
        n_selected = len(run_stats.get("selected_zips", []))
        est_calls = n_selected * PLACES_CALLS_PER_ZIP
        logger.info(
            "[dry-run] National ZIPs=%s | candidates after gate=%s | "
            "would call Places on=%s ZIPs | est calls=%s (%s x %s) | "
            "est cost ~$%.0f at $0.032/call. No file written."
            % (run_stats.get("total_national_zips", len(payload.get("rows") or [])),
               run_stats.get("candidate_zips", "n/a"),
               n_selected, est_calls, n_selected, PLACES_CALLS_PER_ZIP,
               est_calls * 0.032))
        if run_stats.get("filtered_out"):
            logger.info("[dry-run] Filtered out by reason: %s"
                        % run_stats["filtered_out"])
        return 0

    out = write_enriched_payload(payload, Path(args.output), all_enriched)
    logger.info(f"Wrote enriched national layer ({len(enriched)} enriched ZIPs) "
                f"→ {out}")
    if args.use_places and hasattr(places_client, "summary"):
        places_summary = places_client.summary()
        summary_path = write_enrichment_cache_summary(
            Path(args.cache_summary_output),
            input_path=in_path,
            enriched_output_path=out,
            selected_zips=run_stats.get("selected_zips", []),
            enriched_zips=enriched,
            estimated_places_calls=run_stats.get("estimated_places_calls", 0),
            places_summary=places_summary,
            started_at=started_at,
        )
        logger.info(
            "Places call report: "
            f"{places_summary['live_places_calls']} live, "
            f"{places_summary['cache_hits']} cache hits, "
            f"{places_summary['cache_stale']} stale, "
            f"{places_summary['cache_misses']} misses → {summary_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
