"""
ALLCPR Site Intelligence — centralized configuration.

Copy `.env.example` to `.env` and fill in your keys. Nothing in this file
hardcodes secrets; everything sensitive comes from the environment.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional


# --------------------------------------------------------------------------- #
# Secrets / external services
# --------------------------------------------------------------------------- #

PRODUCT_NAME: str = "ALLCPR Site Intelligence"
PRODUCT_VERSION: str = "v2.0"
PRODUCT_STATUS: str = "Internal decision-support product"

GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
CENSUS_API_KEY: str = os.getenv("CENSUS_API_KEY", "")  # optional; ACS works without
BLS_API_KEY: str = os.getenv("BLS_API_KEY", "")  # optional; stubbed for now
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")  # optional; summary only
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Groq — a free, OpenAI-compatible LLM provider (console.groq.com). Used for
# the same optional AI executive summary. Both OpenAI and Groq speak the
# /chat/completions API, so one client serves either.
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Which LLM provider to use for AI summaries: "groq" | "openai" | "" (auto).
# Auto prefers Groq (free) when a GROQ_API_KEY is present, else OpenAI. The
# AI summary is ALWAYS optional — the pipeline runs identically without it,
# and the model is constrained to rephrase the deterministic report, never
# to invent figures.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "").strip().lower()
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "30"))

# --------------------------------------------------------------------------- #
# Network behavior
# --------------------------------------------------------------------------- #

REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "20"))
RATE_LIMIT_SECONDS: float = float(os.getenv("RATE_LIMIT_SECONDS", "1.0"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "4"))
RETRY_BACKOFF_SECONDS: float = float(os.getenv("RETRY_BACKOFF_SECONDS", "2.0"))

# Competitor website analysis is intentionally tiny: homepage + at most one
# obvious classes/booking/schedule page.
COMPETITOR_WEBSITE_ANALYSIS_ENABLED: bool = (
    os.getenv("COMPETITOR_WEBSITE_ANALYSIS_ENABLED", "true").lower() != "false"
)
COMPETITOR_WEBSITE_TIMEOUT: float = float(
    os.getenv("COMPETITOR_WEBSITE_TIMEOUT", "5")
)

# --------------------------------------------------------------------------- #
# Search / sampling defaults
# --------------------------------------------------------------------------- #

DEFAULT_RADIUS_MILES: float = float(os.getenv("DEFAULT_RADIUS_MILES", "5"))
GRID_SPACING_MILES: float = float(os.getenv("GRID_SPACING_MILES", "2.5"))
MAX_CANDIDATES_PER_CITY: int = int(os.getenv("MAX_CANDIDATES_PER_CITY", "12"))
METRO_DEDUPE_DISTANCE_MILES: float = float(
    os.getenv("METRO_DEDUPE_DISTANCE_MILES", "1.0")
)

# Hydrate this many top competitors with Place Details (phone/website/hours).
COMPETITOR_HYDRATE_TOP_N: int = int(os.getenv("COMPETITOR_HYDRATE_TOP_N", "5"))
# How many demand-driver places per category to keep with full PlaceProfile.
DEMAND_TOP_N_PER_CATEGORY: int = int(os.getenv("DEMAND_TOP_N_PER_CATEGORY", "5"))

# Photo URL safety: if true, never embed the API key in saved reports.
SAFE_PHOTO_URLS: bool = os.getenv("SAFE_PHOTO_URLS", "true").lower() != "false"

# --------------------------------------------------------------------------- #
# Profitability assumptions
# Configurable revenue model inputs. These are POLICY, not measured truth.
# The profitability section of the report labels every number as "estimated".
# --------------------------------------------------------------------------- #

AVG_CPR_COURSE_PRICE: float = float(os.getenv("AVG_CPR_COURSE_PRICE", "85"))
AVG_BLS_COURSE_PRICE: float = float(os.getenv("AVG_BLS_COURSE_PRICE", "95"))
AVG_STUDENTS_PER_CLASS: float = float(os.getenv("AVG_STUDENTS_PER_CLASS", "8"))
CLASSES_PER_WEEK_LOW: float = float(os.getenv("CLASSES_PER_WEEK_LOW", "3"))
CLASSES_PER_WEEK_MID: float = float(os.getenv("CLASSES_PER_WEEK_MID", "6"))
CLASSES_PER_WEEK_HIGH: float = float(os.getenv("CLASSES_PER_WEEK_HIGH", "10"))

# --------------------------------------------------------------------------- #
# Directory layout
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
ENRICHED_DIR = DATA_DIR / "enriched"
SCORED_DIR = DATA_DIR / "scored"
REPORTS_DIR = DATA_DIR / "reports"
# Machine-readable outputs consumed by the web dashboard (latest_report.json).
PROCESSED_DIR = DATA_DIR / "processed"

# Real, gitignored source CSVs (roster, past-instructor performance, locations,
# revenue health, competitor pricing, demand). Overridable so a hosted instance
# can read them off the persistent disk — the repo checkout never carries them.
# On Render, set MANUAL_DATA_DIR=/opt/render/project/src/data/cache/manual.
MANUAL_DIR = Path(os.environ.get("MANUAL_DATA_DIR") or (DATA_DIR / "manual"))

for _d in (RAW_DIR, ENRICHED_DIR, SCORED_DIR, REPORTS_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Scoring weights — must sum to 1.0
# --------------------------------------------------------------------------- #

SCORE_WEIGHTS: Dict[str, float] = {
    "demand_score": 0.23,
    "healthcare_training_ecosystem_score": 0.18,
    "competition_gap_score": 0.15,
    "allcpr_opportunity_score": 0.13,
    "economy_score": 0.09,
    "accessibility_score": 0.10,
    "historical_performance_score": 0.08,
    "profitability_score": 0.04,
}

assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-6, "SCORE_WEIGHTS must sum to 1.0"


# --------------------------------------------------------------------------- #
# Scoring tunables — calibration knobs, env-overridable.
#
# These are the dials you'd turn against a real back-test (Enrollware
# outcomes). They live here, not buried in module literals, so tuning never
# requires touching scoring logic. Every value has a documented default and an
# env override for experimentation.
# --------------------------------------------------------------------------- #

# How much weight cohort-relative z-scores get vs. the absolute score in
# cohort_normalization. 0.0 = legacy absolute-only; 1.0 = cohort wins entirely.
COHORT_BLEND: float = float(os.getenv("COHORT_BLEND", "0.5"))

# Global multiplier applied to every demand/training saturation cap. Raising it
# makes dense metros harder to saturate (more inter-candidate spread); lowering
# it makes thin markets saturate sooner. 1.0 = use the calibrated table as-is.
DEMAND_CAP_MULTIPLIER: float = float(os.getenv("DEMAND_CAP_MULTIPLIER", "1.0"))

# Rent-pressure index normalization bounds (low, high) per signal. A value at
# or below `low` contributes 0; at or above `high` contributes 1.0.
RENT_PRESSURE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "income": (
        float(os.getenv("RENT_PRESSURE_INCOME_LOW", "40000")),
        float(os.getenv("RENT_PRESSURE_INCOME_HIGH", "160000")),
    ),
    "density": (
        float(os.getenv("RENT_PRESSURE_DENSITY_LOW", "10")),
        float(os.getenv("RENT_PRESSURE_DENSITY_HIGH", "250")),
    ),
    "competition": (
        float(os.getenv("RENT_PRESSURE_COMPETITION_LOW", "2")),
        float(os.getenv("RENT_PRESSURE_COMPETITION_HIGH", "40")),
    ),
    # Business-corridor proximity in miles: 0mi = max premium, this many mi = 0.
    "corridor_miles": (
        0.0,
        float(os.getenv("RENT_PRESSURE_CORRIDOR_MILES", "5")),
    ),
}


# --------------------------------------------------------------------------- #
# Site-validation + business-feasibility assumptions (POLICY, env-overridable).
#
# Used by scoring/business_feasibility.py and scoring/candidate_type.py. Every
# dollar figure is an ESTIMATE; the report labels feasibility output as such.
# --------------------------------------------------------------------------- #

# Per-student variable cost (instructor time, cards, manikin consumables).
VARIABLE_COST_PER_STUDENT: float = float(os.getenv("VARIABLE_COST_PER_STUDENT", "25"))

# Monthly fixed-cost band for a single small training center (rent + utilities
# + base staffing + insurance). Midpoint is used for break-even.
FIXED_COST_MONTHLY_LOW: float = float(os.getenv("FIXED_COST_MONTHLY_LOW", "4000"))
FIXED_COST_MONTHLY_HIGH: float = float(os.getenv("FIXED_COST_MONTHLY_HIGH", "9000"))

# Classroom capacity assumptions (students per session) for classroom_fit.
CLASSROOM_MIN_STUDENTS: int = int(os.getenv("CLASSROOM_MIN_STUDENTS", "6"))
CLASSROOM_TARGET_STUDENTS: int = int(os.getenv("CLASSROOM_TARGET_STUDENTS", "12"))
# Square feet per student rule-of-thumb for classroom_fit from a listing sqft.
SQFT_PER_STUDENT: float = float(os.getenv("SQFT_PER_STUDENT", "35"))

# Commercial-listing manual override file (proprietary; real file gitignored).
COMMERCIAL_OVERRIDES_FILE: Path = RAW_DIR / "commercial_overrides.csv"

# Competition-pressure band cutoffs on competition_pressure_score (0..100).
# score < LOW → Low; < MEDIUM → Medium; < EXTREME → High; >= EXTREME → Extreme.
PRESSURE_BAND_LOW: float = float(os.getenv("PRESSURE_BAND_LOW", "25"))
PRESSURE_BAND_MEDIUM: float = float(os.getenv("PRESSURE_BAND_MEDIUM", "50"))
PRESSURE_BAND_EXTREME: float = float(os.getenv("PRESSURE_BAND_EXTREME", "78"))


# --------------------------------------------------------------------------- #
# National ZIP-level MODELED opportunity score (POLICY, env-overridable).
#
# This drives the "Modeled national demand" dashboard layer — a public-data
# ESTIMATE of opportunity for every US ZIP, kept entirely separate from the
# real Enrollware history layer. Every bound below is policy, not measured
# truth; the dashboard labels the score as a modeled estimate.
#
# Normalization mirrors economy_score._norm: a raw value at/below `low`
# contributes 0, at/above `high` contributes 1.0. Missing signals drop out and
# the weighted sum renormalizes over whatever is present (never invented).
#
# Two course "tilts" are derivable from public data — a healthcare-workforce
# (BLS) emphasis and a community/layperson (CPR) emphasis. Brand (AHA vs ARC)
# is NOT modeled: no public dataset encodes brand preference.
# --------------------------------------------------------------------------- #

ZIP_MODEL_BOUNDS: Dict[str, Tuple[float, float]] = {
    # Baseline signals (ACS ZCTA + Gazetteer) — always available nationally.
    "population":                  (500.0, 50_000.0),
    "population_density":          (50.0, 8_000.0),     # people / sq mi
    "median_household_income":     (40_000.0, 150_000.0),
    "working_age_share":           (0.55, 0.80),
    "employment_rate":             (0.50, 0.75),
    "bachelors_or_higher_share":   (0.15, 0.55),
    "healthcare_employment_share": (0.05, 0.25),
    # Enrichment signals (Phase 2; only present for enriched ZIPs).
    "healthcare_facility_density": (0.0, 15.0),
    "community_facility_density":  (0.0, 25.0),
    "training_school_density":     (0.0, 8.0),
    "competition_gap_score":       (0.0, 100.0),        # high = little competition
}

# Weight maps per course tilt. Baseline signals sum to ~0.80; enrichment signals
# fill the remaining ~0.20 and only count for enriched ZIPs (weights renormalize
# when absent). Each full map sums to 1.0.
ZIP_MODELED_WEIGHTS_BLS: Dict[str, float] = {
    "healthcare_employment_share": 0.28,
    "working_age_share":           0.14,
    "median_household_income":     0.10,
    "employment_rate":             0.08,
    "population":                  0.08,
    "population_density":          0.07,
    "bachelors_or_higher_share":   0.05,
    # enrichment (Phase 2)
    "healthcare_facility_density": 0.10,
    "training_school_density":     0.05,
    "competition_gap_score":       0.05,
}
assert abs(sum(ZIP_MODELED_WEIGHTS_BLS.values()) - 1.0) < 1e-6

ZIP_MODELED_WEIGHTS_CPR: Dict[str, float] = {
    "population":                  0.20,
    "population_density":          0.18,
    "median_household_income":     0.12,
    "working_age_share":           0.10,
    "healthcare_employment_share": 0.10,
    "employment_rate":             0.05,
    "bachelors_or_higher_share":   0.05,
    # enrichment (Phase 2)
    "community_facility_density":  0.10,
    "training_school_density":     0.05,
    "competition_gap_score":       0.05,
}
assert abs(sum(ZIP_MODELED_WEIGHTS_CPR.values()) - 1.0) < 1e-6


# --------------------------------------------------------------------------- #
# Enhanced (Phase-2) modeled signal enrichment (POLICY, env-overridable).
#
# These tune how Google Places / POI evidence is turned into the three enhanced
# modeled signals (healthcare_facility_density, training_school_density,
# competition_gap_score). They are deliberately separate, documented dials so a
# reproducible enrichment run never bakes a magic number into scoring logic.
# --------------------------------------------------------------------------- #

# Search radius (miles) for the Places nearby/text queries that feed the density
# signals. POIs within this radius of the ZIP centroid count as "in or near" it.
ENHANCED_SIGNAL_RADIUS_MILES: float = float(
    os.getenv("ENHANCED_SIGNAL_RADIUS_MILES", "5.0"))

# competition_gap_score formula: competitor_count is converted to a 0..1 gap as
# `1 - min(1, competitor_count / saturation_count)`, then scaled to 0..100 to
# match ZIP_MODEL_BOUNDS["competition_gap_score"]. At/above this many direct
# CPR/BLS/first-aid competitors the market is treated as saturated (gap → 0);
# competitor_count near 0 → gap near 1.0 (score → 100). Transparent + tunable.
COMPETITION_SATURATION_COUNT: float = float(
    os.getenv("COMPETITION_SATURATION_COUNT", "20"))


# --------------------------------------------------------------------------- #
# Demand & competition category catalog.
# --------------------------------------------------------------------------- #

DEMAND_CATEGORIES: List[Dict[str, str]] = [
    {"key": "hospital",            "type": "hospital",     "keyword": ""},
    {"key": "urgent_care",         "type": "",             "keyword": "urgent care"},
    {"key": "fire_station",        "type": "fire_station", "keyword": ""},
    {"key": "ems",                 "type": "",             "keyword": "ambulance EMS station"},
    {"key": "nursing_school",      "type": "",             "keyword": "nursing school"},
    {"key": "medical_school",      "type": "",             "keyword": "medical school"},
    {"key": "dental_school",       "type": "",             "keyword": "dental school"},
    {"key": "community_college",   "type": "",             "keyword": "community college"},
    {"key": "university",          "type": "university",   "keyword": ""},
    {"key": "childcare_center",    "type": "",             "keyword": "childcare daycare"},
    {"key": "senior_care",         "type": "",             "keyword": "assisted living senior care"},
    {"key": "gym",                 "type": "gym",          "keyword": ""},
    {"key": "physical_therapy",    "type": "",             "keyword": "physical therapy clinic"},
    {"key": "dental_clinic",       "type": "dentist",      "keyword": ""},
    {"key": "medical_clinic",      "type": "",             "keyword": "medical clinic"},
    {"key": "emt_training",        "type": "",             "keyword": "EMT training program"},
    {"key": "cna_training",        "type": "",             "keyword": "CNA training program"},
    {"key": "healthcare_training", "type": "",             "keyword": "healthcare training school"},
]

# Categories that count toward the "healthcare training ecosystem" sub-score.
TRAINING_ECOSYSTEM_KEYS = (
    "nursing_school", "medical_school", "dental_school",
    "community_college", "university",
    "emt_training", "cna_training", "healthcare_training",
)

COMPETITION_QUERIES: List[str] = [
    "CPR training",
    "BLS training",
    "First Aid training",
    "American Heart Association training center",
    "Red Cross CPR class",
    "CPR certification",
    "EMT school",
    "medical training center",
]

# Categories used to find a *meaningful* anchor for each candidate point.
# Tried in order; the closest commercially-viable hit wins. Pulled
# transit_station out — in dense urban areas it produces BART/bus stations
# and intersection-style names that are not leasable storefronts.
ANCHOR_QUERIES: List[Dict[str, str]] = [
    {"type": "shopping_mall",     "label": "shopping center"},
    {"type": "supermarket",       "label": "supermarket / retail anchor"},
    {"type": "university",        "label": "university / college"},
    {"type": "hospital",          "label": "hospital"},
    {"keyword": "medical plaza",       "label": "medical office plaza"},
    {"keyword": "medical office building", "label": "medical office building"},
    {"keyword": "office building",     "label": "office building"},
    {"keyword": "business park",       "label": "business park"},
    {"keyword": "retail plaza",        "label": "retail plaza"},
    {"keyword": "shopping plaza",      "label": "shopping plaza"},
    {"keyword": "training center",     "label": "training center"},
    {"keyword": "coworking",           "label": "coworking space"},
]

# Maximum distance (miles) we'll consider an anchor "associated with" the
# candidate point. Beyond this we fall back to reverse-geocoded address.
ANCHOR_MAX_DISTANCE_MILES: float = float(os.getenv("ANCHOR_MAX_DISTANCE_MILES", "0.75"))

# Distance buckets used for "nearby_X_Nmi" counts.
DISTANCE_BUCKETS_MILES = (1, 3, 5, 10)


# --------------------------------------------------------------------------- #
# Cache + freshness layer
# --------------------------------------------------------------------------- #

CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "true").lower() != "false"
CACHE_DIR: Path = DATA_DIR / "cache"
CACHE_DB: Path = CACHE_DIR / "cache.sqlite"

# Per-source TTLs in seconds (env-overridable as DAYS for readability).
def _ttl_days_env(var: str, default_days: int) -> int:
    try:
        days = int(os.getenv(var, str(default_days)))
    except ValueError:
        days = default_days
    return days * 86400


CACHE_TTL_DEFAULT_SECONDS: int = _ttl_days_env("CACHE_TTL_DEFAULT_DAYS", 30)

CACHE_TTLS: Dict[str, int] = {
    "google_places:nearby_search": _ttl_days_env("CACHE_TTL_NEARBY_SEARCH_DAYS", 30),
    "google_places:text_search":   _ttl_days_env("CACHE_TTL_TEXT_SEARCH_DAYS", 14),
    "google_places:place_details": _ttl_days_env("CACHE_TTL_PLACE_DETAILS_DAYS", 30),
    "census:fetch_demographics":   _ttl_days_env("CACHE_TTL_CENSUS_DAYS", 365),
    "bls_qcew:fetch_qcew":         _ttl_days_env("CACHE_TTL_BLS_QCEW_DAYS", 365),
}


def ttl_for(provider: str, method: str) -> int:
    """Return the configured TTL in seconds, or the default if unknown."""
    return CACHE_TTLS.get(f"{provider}:{method}", CACHE_TTL_DEFAULT_SECONDS)
