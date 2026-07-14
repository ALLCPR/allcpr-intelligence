"""
Offline ZIP prefilter for live API enrichment.

Google Places is useful for finalists, but it is too slow, expensive, and
page-capped to run across every modeled ZIP. This module scores ZIP rows using
cheap local signals only, then labels which ZIPs are worth live API calls.

Strategy note: the Places scoring backtest was effectively flat (overall
correlation 0.103 → 0.105) and dense ZIPs saturated near the 20-result search
cap. This gate is therefore for finalist context/validation runs, not for
turning Places into the default national scoring engine.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional


MIN_POPULATION = float(os.getenv("API_FILTER_MIN_POPULATION", "5000"))
MIN_POPULATION_DENSITY = float(os.getenv("API_FILTER_MIN_POP_DENSITY", "300"))
MIN_OVERALL = float(os.getenv("API_FILTER_MIN_OVERALL", "45"))
LOW_CONFIDENCE_VALUES = {"missing", "low", "very_low", "very low", "poor"}

PLACES_CALLS_PER_ZIP = 4
SECONDS_PER_PLACES_CALL = float(os.getenv("RATE_LIMIT_SECONDS", "1.0"))


HEALTHCARE_FIELDS = (
    "hospital_count", "urgent_care_count", "ems_fire_count",
    "healthcare_facility_count", "healthcare_provider_count", "nurse_count",
    "physician_count", "clinic_provider_count", "provider_density_per_10k_pop",
    "healthcare_facility_density",
)
EDUCATION_FIELDS = (
    "college_count", "nursing_school_count", "health_program_school_count",
    "student_enrollment_count",
)
COMMUNITY_FIELDS = (
    "childcare_count", "school_count", "community_facility_count",
    "community_facility_density",
)
BULK_SIGNAL_FIELDS = HEALTHCARE_FIELDS + EDUCATION_FIELDS + COMMUNITY_FIELDS + (
    "parking_proxy_score", "commercial_access_proxy_score",
)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return out


def _norm(value: Any, low: float, high: float) -> float:
    val = _num(value)
    if val is None or high <= low:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (val - low) / (high - low)))


def _max_norm(row: Dict[str, Any], caps: Dict[str, float]) -> float:
    return max((_norm(row.get(field), 0.0, cap) for field, cap in caps.items()),
               default=0.0)


def _has_bulk_enrichment(row: Dict[str, Any]) -> bool:
    sources = {str(s).upper() for s in (row.get("enrichment_sources") or [])}
    if sources & {"HIFLD", "NPI", "IPEDS", "OSM"}:
        return True
    if row.get("enrichment_tier") == "bulk_enriched":
        return True
    return any(field in row for field in BULK_SIGNAL_FIELDS)


def _has_meaningful_bulk_signal(row: Dict[str, Any]) -> bool:
    return any((_num(row.get(field), 0.0) or 0.0) > 0.0
               for field in BULK_SIGNAL_FIELDS)


def _hard_exclusion_reason(row: Dict[str, Any]) -> Optional[str]:
    if _num(row.get("lat")) is None or _num(row.get("lng")) is None:
        return "Missing latitude/longitude."
    pop = _num(row.get("population"), 0.0) or 0.0
    if pop <= 0:
        return "Population is zero or missing."
    if pop < MIN_POPULATION:
        return f"Population below {MIN_POPULATION:,.0f}."
    density = _num(row.get("population_density"), 0.0) or 0.0
    if density < MIN_POPULATION_DENSITY:
        return f"Population density below {MIN_POPULATION_DENSITY:,.0f}/sq mi."
    overall = _num(row.get("overall"), 0.0) or 0.0
    if overall < MIN_OVERALL:
        return f"Modeled overall demand below {MIN_OVERALL:.0f}."
    confidence = str(row.get("data_confidence") or "").strip().lower()
    if confidence in LOW_CONFIDENCE_VALUES:
        return f"Data confidence is too low ({confidence})."
    if _has_bulk_enrichment(row) and not _has_meaningful_bulk_signal(row):
        return ("Bulk enrichment is present but has no healthcare, education, "
                "provider, community, or commercial access signal.")
    return None


def _healthcare_score(row: Dict[str, Any]) -> float:
    bulk_score = _max_norm(row, {
        "hospital_count": 5,
        "urgent_care_count": 5,
        "ems_fire_count": 10,
        "healthcare_facility_count": 12,
        "healthcare_provider_count": 150,
        "provider_density_per_10k_pop": 50,
        "physician_count": 50,
        "nurse_count": 75,
        "clinic_provider_count": 25,
        "healthcare_facility_density": 12,
    })
    workforce_score = _norm(row.get("healthcare_employment_share"), 0.0, 0.03)
    return round(max(bulk_score, 0.8 * workforce_score), 1)


def _education_score(row: Dict[str, Any]) -> float:
    bulk_score = _max_norm(row, {
        "college_count": 5,
        "nursing_school_count": 3,
        "health_program_school_count": 5,
        "student_enrollment_count": 10_000,
    })
    attainment_score = _norm(row.get("bachelors_or_higher_share"), 0.15, 0.50)
    return round(max(bulk_score, 0.7 * attainment_score), 1)


def _community_score(row: Dict[str, Any]) -> float:
    bulk_score = _max_norm(row, {
        "childcare_count": 20,
        "school_count": 20,
        "community_facility_count": 20,
        "community_facility_density": 20,
    })
    density_score = _norm(row.get("population_density"), MIN_POPULATION_DENSITY, 5_000)
    population_score = _norm(row.get("population"), MIN_POPULATION, 100_000)
    baseline_score = max(density_score, 0.7 * population_score)
    return round(max(bulk_score, baseline_score), 1)


def _commercial_score(row: Dict[str, Any]) -> float:
    score = _norm(row.get("commercial_access_proxy_score"), 0.0, 100.0)
    score = max(score, 0.6 * _norm(row.get("population_density"),
                                   MIN_POPULATION_DENSITY, 5_000))
    if (row.get("commercial") or {}).get("commercial_validated"):
        score = max(score, 100.0)
    if row.get("commercial_space_available"):
        score = max(score, 80.0)
    return round(score, 1)


def _historical_score(row: Dict[str, Any]) -> float:
    return round(max(
        _norm(row.get("total_students"), 0.0, 250.0),
        _norm(row.get("class_count") or row.get("classes"), 0.0, 30.0),
        _norm(row.get("avg_students"), 0.0, 15.0),
        _norm(row.get("fill_rate"), 40.0, 85.0),
        _norm(row.get("demand_score"), 0.0, 100.0),
    ), 1)


def compute_api_candidate_score(row: Dict[str, Any]) -> float:
    """Return a 0-100 offline score for whether live API enrichment is worth it."""
    if _hard_exclusion_reason(row):
        return 0.0
    overall = _norm(row.get("overall"), 0.0, 100.0)
    score = (
        0.35 * overall
        + 0.20 * _healthcare_score(row)
        + 0.15 * _education_score(row)
        + 0.10 * _community_score(row)
        + 0.10 * _commercial_score(row)
        + 0.10 * _historical_score(row)
    )
    return round(score, 1)


def classify_api_candidate(row: Dict[str, Any]) -> str:
    """Classify a ZIP for live API enrichment: exclude/low/medium/high/finalist."""
    if _hard_exclusion_reason(row):
        return "exclude"
    score = compute_api_candidate_score(row)
    if score >= 60:
        return "finalist"
    if score >= 50:
        return "high"
    if score >= 40:
        return "medium"
    if score >= 30:
        return "low"
    return "exclude"


def _reason_for_candidate(row: Dict[str, Any], score: float, priority: str) -> str:
    positives: List[str] = []
    if (_num(row.get("overall"), 0.0) or 0.0) >= 60:
        positives.append("strong modeled demand")
    if (_num(row.get("population_density"), 0.0) or 0.0) >= 2_000:
        positives.append("good population density")
    if _healthcare_score(row) >= 40:
        positives.append("healthcare/provider signals present")
    if _education_score(row) >= 35:
        positives.append("education/nursing-school signals present")
    if _community_score(row) >= 35:
        positives.append("community/childcare/school signals present")
    if _commercial_score(row) >= 50:
        positives.append("commercial access signal present")
    if _historical_score(row) >= 50:
        positives.append("historical ALLCPR signal present")
    if not positives:
        positives.append("passes baseline population, density, demand, and confidence gates")
    return f"{priority.title()} API candidate ({score:.1f}/100): " + ", ".join(positives) + "."


def explain_api_filter(row: Dict[str, Any]) -> str:
    """Human-readable reason for excluding or selecting a ZIP."""
    hard_reason = _hard_exclusion_reason(row)
    if hard_reason:
        return f"Excluded from live API enrichment: {hard_reason}"
    priority = classify_api_candidate(row)
    score = compute_api_candidate_score(row)
    if priority == "exclude":
        return f"Excluded from live API enrichment: weak API candidate score ({score:.1f}/100)."
    return _reason_for_candidate(row, score, priority)


def annotate_api_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy with API filter fields attached."""
    out = dict(row)
    score = compute_api_candidate_score(row)
    priority = classify_api_candidate(row)
    out["api_candidate_score"] = score
    out["api_priority"] = priority
    out["api_filter_reason"] = explain_api_filter(row)
    out["recommended_for_live_places"] = priority in {"high", "finalist"}
    return out


def filter_api_candidates(
    rows: Iterable[Dict[str, Any]],
    max_zips: Optional[int] = None,
    min_score: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Return non-excluded ZIP rows, ranked by API-candidate score descending."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        annotated = annotate_api_candidate(row)
        if annotated["api_priority"] == "exclude":
            continue
        if min_score is not None and annotated["api_candidate_score"] < min_score:
            continue
        out.append(annotated)
    out.sort(key=lambda r: (r.get("api_candidate_score") or 0.0,
                            r.get("overall") or 0.0,
                            str(r.get("zip") or "")), reverse=True)
    if max_zips is not None:
        out = out[:max(0, int(max_zips))]
    return out


def estimated_places_calls(zip_count: int) -> int:
    return max(0, int(zip_count)) * PLACES_CALLS_PER_ZIP


def estimated_runtime_minutes(zip_count: int) -> int:
    seconds = estimated_places_calls(zip_count) * max(SECONDS_PER_PLACES_CALL, 0.0)
    return int(round(seconds / 60.0))
