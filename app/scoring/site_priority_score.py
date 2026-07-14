"""v2.1 site-priority scoring layer for modeled ZIP records.

This module is intentionally separate from ``zip_modeled_opportunity``. The
legacy Opportunity Score remains the market estimate used by the v2.0 map; this
layer adds decision-support scores for site-opening workflow screening.

No external APIs are called here. Every function is deterministic and treats
missing evidence as missing, partial, or neutral/provisional rather than as a
hard rejection.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCORE_FORMULA_VERSION = "v2.1"
UNKNOWN_COMMERCIAL_SCORE = 50.0
UNKNOWN_HISTORICAL_FIT_SCORE = 50.0

COURSE_LABELS = {
    "overall": "Overall",
    "aha_bls": "AHA BLS",
    "arc_bls": "ARC BLS",
    "arc_cpr": "ARC CPR",
}

COMPETITION_RISK_LABELS = {
    "unknown": "Competition evidence incomplete",
    "unproven_market": "Unproven market",
    "low_to_moderate": "Low-to-moderate competition",
    "competitive_but_healthy": "Competitive but healthy",
    "competitive": "Competitive market",
    "saturated_unless_differentiated": "Saturated unless differentiated",
}

# Weighted components behind final_site_priority_score, in display order.
# (key, operator-facing label, score field on `scores`, weight)
SITE_PRIORITY_COMPONENTS = (
    ("market_demand", "Market demand", "market_demand_score", 0.45),
    ("validation_evidence", "Validation evidence", "validation_evidence_score", 0.25),
    ("commercial_feasibility", "Commercial feasibility", "commercial_feasibility_score_used", 0.20),
    ("historical_fit", "ALLCPR historical fit", "historical_allcpr_fit_score_used", 0.10),
)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _first_number(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _truthy(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "available", "validated", "ready"}:
        return True
    if text in {"false", "no", "n", "0", "none", "unavailable"}:
        return False
    return None


def _clamp(value: Optional[float], low: float = 0.0,
           high: float = 100.0) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return max(low, min(high, value))


def _round_score(value: Optional[float]) -> Optional[float]:
    value = _clamp(value)
    return None if value is None else round(value, 1)


def _linear_score(value: Any, low: float, high: float) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    if high <= low:
        return None
    return _round_score(100.0 * (number - low) / (high - low))


def _log_count_score(value: Any, cap: float) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    count = max(0.0, number)
    cap = max(1.0, cap)
    return _round_score(100.0 * math.log1p(count) / math.log1p(cap))


def _weighted_score(parts: Sequence[Tuple[Optional[float], float]]) -> Optional[float]:
    weighted = 0.0
    used = 0.0
    for value, weight in parts:
        score = _clamp(value)
        if score is None or weight <= 0:
            continue
        weighted += score * weight
        used += weight
    if used <= 0:
        return None
    return _round_score(weighted / used)


def _score_count_density(
    count: Optional[float],
    density: Optional[float],
    *,
    count_cap: float,
    density_cap: float,
    count_weight: float = 0.55,
) -> Optional[float]:
    return _weighted_score((
        (_log_count_score(count, count_cap), count_weight),
        (_linear_score(density, 0.0, density_cap), 1.0 - count_weight),
    ))


def _score_band(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    if score >= 75:
        return "strong"
    if score >= 55:
        return "moderate"
    if score >= 40:
        return "limited"
    return "weak"


def _display_band(score: Optional[float]) -> str:
    """Human-readable band for operator-facing text."""
    if score is None:
        return "data not provided"
    if score >= 75:
        return "strong"
    if score >= 55:
        return "moderate"
    if score >= 40:
        return "limited"
    return "weak"


def _confidence_label(score: Optional[float]) -> str:
    if score is None:
        return "Low"
    if score >= 82:
        return "High"
    if score >= 65:
        return "Medium-high"
    if score >= 45:
        return "Medium"
    return "Low"


def readable_competition_risk(risk: Any) -> str:
    """Return a dashboard-safe label for an internal competition risk code."""
    return COMPETITION_RISK_LABELS.get(
        str(risk or "unknown"),
        str(risk or "Competition evidence incomplete").replace("_", " ").title(),
    )


def calculate_historical_allcpr_fit_score(row: Dict[str, Any]) -> Optional[float]:
    """Return historical ALLCPR fit when real history or calibration exists."""
    direct = _first_number(
        row,
        "historical_allcpr_fit_score",
        "proven_demand_score",
        "historical_demand_score",
        "allcpr_historical_demand_score",
    )
    if direct is not None:
        return _round_score(direct)

    if str(row.get("historical_status") or "").lower() == "has_allcpr_history":
        demand = _first_number(row, "demand_score", "overall_score", "overall")
        if demand is not None:
            return _round_score(demand)

    agreement = str(row.get("model_agreement") or "").strip().lower()
    agreement_scores = {
        "model_agrees_high": 80.0,
        "hidden_opportunity": 75.0,
        "model_underpredicts": 70.0,
        "mixed": 55.0,
        "insufficient_history": 50.0,
        "model_overpredicts": 40.0,
        "model_agrees_low": 30.0,
    }
    return agreement_scores.get(agreement)


def _places_support_score(row: Dict[str, Any]) -> Optional[float]:
    healthcare = _score_count_density(
        _first_number(row, "healthcare_facility_count", "healthcare_poi_count",
                      "medical_office_count"),
        _first_number(row, "healthcare_facility_density"),
        count_cap=60.0,
        density_cap=15.0,
    )
    training = _score_count_density(
        _first_number(row, "training_school_count", "nursing_school_count",
                      "health_program_school_count"),
        _first_number(row, "training_school_density"),
        count_cap=45.0,
        density_cap=8.0,
    )
    community = _score_count_density(
        _first_number(row, "community_facility_count"),
        _first_number(row, "community_facility_density"),
        count_cap=50.0,
        density_cap=25.0,
    )
    return _weighted_score((
        (healthcare, 0.45),
        (training, 0.45),
        (community, 0.10),
    ))


def calculate_market_demand_score(row: Dict[str, Any]) -> Optional[float]:
    """Score likely student demand without letting Places dominate."""
    population_density = _weighted_score((
        (_linear_score(_first_number(row, "population"), 5_000.0, 60_000.0), 0.50),
        (_linear_score(_first_number(row, "population_density"), 500.0, 10_000.0),
         0.50),
    ))
    workforce = _weighted_score((
        (_linear_score(row.get("working_age_share"), 0.55, 0.80), 0.50),
        (_linear_score(row.get("employment_rate"), 0.50, 0.75), 0.50),
    ))
    healthcare_workforce = _weighted_score((
        (_first_number(row, "bls_demand", "aha_bls_score", "arc_bls_score"), 0.75),
        (_linear_score(row.get("healthcare_employment_share"), 0.005, 0.15), 0.25),
    ))
    community_cpr = _first_number(row, "cpr_demand", "arc_cpr_score")
    income_education = _weighted_score((
        (_linear_score(_first_number(row, "median_income",
                                     "median_household_income"),
                       45_000.0, 160_000.0), 0.60),
        (_linear_score(row.get("bachelors_or_higher_share"), 0.15, 0.55), 0.40),
    ))
    historical_fit = calculate_historical_allcpr_fit_score(row)
    places_support = _places_support_score(row)

    return _weighted_score((
        (population_density, 0.20),
        (workforce, 0.15),
        (healthcare_workforce, 0.20),
        (community_cpr, 0.15),
        (income_education, 0.10),
        (historical_fit, 0.10),
        (places_support, 0.10),
    ))


def calculate_competition_profile(row: Dict[str, Any]) -> Dict[str, Any]:
    """Separate demand proof from saturation risk."""
    count = _first_number(row, "competitor_count", "competitor_count_total",
                          "nearby_cpr_competitors_5mi")
    rating = _first_number(row, "avg_competitor_rating", "competitor_avg_rating")
    quality_signal = _linear_score(rating, 3.5, 5.0) if rating is not None else None

    if count is None:
        market_validation = None
        penalty = 0.0
        risk = "unknown"
    elif count <= 0:
        market_validation = 25.0
        penalty = 0.0
        risk = "unproven_market"
    elif count <= 5:
        market_validation = 72.0
        penalty = 2.0
        risk = "low_to_moderate"
    elif count <= 12:
        market_validation = 86.0
        penalty = 5.0
        risk = "competitive_but_healthy"
    elif count < 20:
        market_validation = 92.0
        penalty = 9.0
        risk = "competitive"
    else:
        market_validation = 96.0
        penalty = 14.0
        risk = "saturated_unless_differentiated"

    if count is not None and count >= 13 and rating is not None:
        if rating >= 4.7:
            penalty += 2.0
        elif rating < 4.0:
            penalty -= 1.0

    penalty = round(max(0.0, min(18.0, penalty)), 1)
    market_validation = _round_score(market_validation)
    return {
        "competitor_count": None if count is None else round(count, 1),
        "competitor_market_validation_score": market_validation,
        "competition_market_validation_score": market_validation,
        "competition_saturation_penalty": penalty,
        "competition_risk_level": risk,
        "competitor_quality_signal": quality_signal,
    }


def _commercial_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    validated = bool(summary.get("commercial_validated"))
    ready = bool(summary.get("commercial_ready"))
    available = _number(summary.get("available_space_count")) or 0.0
    rent = _first_number(summary, "rent_avg", "rent_min", "rent_max")
    parking = str(summary.get("parking_summary") or "").strip().lower()
    classroom = str(summary.get("classroom_fit_summary") or "").strip().lower()
    sources = summary.get("commercial_sources") or []
    updated = summary.get("commercial_updated_at")

    if not validated:
        return {
            "commercial_feasibility_score": None,
            "commercial_feasibility_score_used": UNKNOWN_COMMERCIAL_SCORE,
            "commercial_feasibility_status": "unknown",
            "commercial_feasibility_confirmed": False,
        }

    if ready:
        score = 75.0
        if available > 1:
            score += 5.0
        if rent is not None:
            score += 5.0
        if parking in {"yes", "mixed"}:
            score += 5.0
        if classroom in {"good", "possible"}:
            score += 5.0
        if sources:
            score += 3.0
        if updated:
            score += 2.0
        status = "confirmed"
    else:
        score = 40.0
        if available > 0:
            score += 10.0
        if rent is not None:
            score += 8.0
        if parking in {"yes", "mixed"}:
            score += 8.0
        if classroom in {"good", "possible"}:
            score += 8.0
        status = "partial"

    score = _round_score(score)
    return {
        "commercial_feasibility_score": score,
        "commercial_feasibility_score_used": score,
        "commercial_feasibility_status": status,
        "commercial_feasibility_confirmed": ready,
    }


def calculate_commercial_feasibility_score(row: Dict[str, Any]) -> Dict[str, Any]:
    """Score operational feasibility while preserving unknown/partial states."""
    summary = row.get("commercial")
    if isinstance(summary, dict) and summary:
        return _commercial_from_summary(summary)

    available = _truthy(row.get("commercial_space_available"))
    ready = _truthy(row.get("commercial_ready"))
    rent = _first_number(row, "estimated_rent", "rent_avg", "monthly_rent")
    rent_source = row.get("rent_source") or row.get("commercial_validation_source")
    parking = _first_number(row, "parking_score", "parking_proxy_score")
    classroom = _truthy(row.get("classroom_fit"))

    evidence = [
        available is not None,
        ready is not None,
        rent is not None,
        bool(rent_source),
        parking is not None,
        classroom is not None,
    ]
    if not any(evidence):
        return {
            "commercial_feasibility_score": None,
            "commercial_feasibility_score_used": UNKNOWN_COMMERCIAL_SCORE,
            "commercial_feasibility_status": "unknown",
            "commercial_feasibility_confirmed": False,
        }

    if available is False:
        score = 25.0
        status = "unavailable"
    else:
        score = 45.0 if available else 35.0
        status = "partial"
    if rent is not None:
        score += 10.0
    if rent_source:
        score += 5.0
    if parking is not None:
        score += max(0.0, min(15.0, parking * 0.15))
    if classroom is True:
        score += 15.0
    elif classroom is False:
        score -= 5.0

    confirmed = bool(available is True and ready is True)
    if confirmed:
        status = "confirmed"
        score = max(score, 80.0)

    score = _round_score(score)
    return {
        "commercial_feasibility_score": score,
        "commercial_feasibility_score_used": score,
        "commercial_feasibility_status": status,
        "commercial_feasibility_confirmed": confirmed,
    }


def _data_freshness_completeness_score(row: Dict[str, Any]) -> Optional[float]:
    confidence = str(row.get("data_confidence") or row.get("confidence") or "").lower()
    if confidence in {"ok", "high", "complete"}:
        completeness = 85.0
    elif confidence in {"partial", "medium"}:
        completeness = 60.0
    elif confidence in {"missing", "low"}:
        completeness = 30.0
    else:
        completeness = None

    updated = row.get("enrichment_updated_at") or row.get("updated_at")
    freshness = None
    if updated:
        # Do not make the score decay with wall-clock time; just reward rows
        # carrying a parseable/generated freshness marker.
        try:
            datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            freshness = 90.0
        except ValueError:
            freshness = 65.0

    return _weighted_score(((completeness, 0.55), (freshness, 0.45)))


def calculate_validation_evidence_score(row: Dict[str, Any]) -> Optional[float]:
    """Score real-world evidence that the ZIP deserves attention."""
    healthcare = _score_count_density(
        _first_number(row, "healthcare_facility_count", "healthcare_poi_count",
                      "medical_office_count"),
        _first_number(row, "healthcare_facility_density"),
        count_cap=60.0,
        density_cap=15.0,
    )
    training = _weighted_score((
        (_score_count_density(
            _first_number(row, "training_school_count",
                          "health_program_school_count"),
            _first_number(row, "training_school_density"),
            count_cap=45.0,
            density_cap=8.0,
        ), 0.75),
        (_log_count_score(row.get("nursing_school_count"), 12.0), 0.25),
    ))
    anchors = _weighted_score((
        (_log_count_score(row.get("hospital_count"), 10.0), 0.55),
        (_log_count_score(row.get("urgent_care_count"), 10.0), 0.45),
    ))
    commercial = calculate_commercial_feasibility_score(row)
    historical = calculate_historical_allcpr_fit_score(row)
    data_quality = _data_freshness_completeness_score(row)
    real_world_parts = (
        healthcare,
        training,
        anchors,
        commercial["commercial_feasibility_score"],
        historical,
    )
    competition = calculate_competition_profile(row)
    market_proof = competition.get("competitor_market_validation_score")
    has_non_data_evidence = any(part is not None for part in real_world_parts)
    if not has_non_data_evidence and market_proof is None:
        return None

    base = _weighted_score((
        (healthcare, 0.25),
        (training, 0.20),
        (anchors, 0.15),
        (commercial["commercial_feasibility_score"], 0.15),
        (historical, 0.15),
        (data_quality, 0.10),
    ))

    if not has_non_data_evidence:
        return _weighted_score(((market_proof, 0.90), (data_quality, 0.10)))
    if base is not None and market_proof is not None:
        return _round_score(base * 0.90 + market_proof * 0.10)
    return base


def _critical_data_missing(row: Dict[str, Any], market: Optional[float],
                           validation: Optional[float]) -> bool:
    if market is not None or validation is not None:
        return False
    return not any(_number(row.get(k)) is not None for k in (
        "overall", "overall_score", "population", "validation_score",
        "healthcare_facility_count", "training_school_count",
    ))


def _decision_label(scores: Dict[str, Any]) -> str:
    final = scores.get("final_site_priority_score")
    market = scores.get("market_demand_score")
    validation = scores.get("validation_evidence_score")
    risk = scores.get("competition_risk_level")
    commercial_confirmed = bool(scores.get("commercial_feasibility_confirmed"))

    if final is None or market is None:
        return "Insufficient data"

    market_value = float(market)
    validation_value = float(validation or 0.0)

    if risk == "saturated_unless_differentiated":
        if market_value < 55:
            return "Saturated / low priority unless strategic"
        if not commercial_confirmed:
            return "Validation-supported opportunity - needs commercial validation"
        return "Demand proven, saturation risk high"

    if not commercial_confirmed:
        if market_value < 55 and validation_value >= 70:
            return "Watchlist / monitor"
        if market_value >= 65 and validation_value >= 70:
            return "Strong candidate, validate commercial fit"
        if validation_value >= 70:
            return "Validation-supported opportunity - needs commercial validation"
        if market_value >= 70 and validation_value < 55:
            return "Needs field test"
        if final >= 55:
            return "Validation-supported opportunity - needs commercial validation"
        if final >= 45:
            return "Watchlist / monitor"
        return "Low priority"

    if market_value >= 65 and validation_value >= 65 and final >= 70:
        if risk in {"low_to_moderate", "competitive_but_healthy", "unknown"}:
            return "Ready for site screening"
    if market_value >= 70 and validation_value < 55:
        return "Needs field test"
    if risk == "competitive" and validation_value >= 65:
        return "Demand proven, saturation risk high"
    if final >= 65:
        return "Ready for site screening"
    if final >= 55:
        return "Validation-supported opportunity"
    if final >= 45:
        return "Watchlist / monitor"
    return "Low priority"


def _next_steps(scores: Dict[str, Any]) -> List[str]:
    steps: List[str] = []
    if not scores.get("commercial_feasibility_confirmed"):
        steps.append("Validate commercial fit: rent, parking, classroom size, and lease availability.")
    if scores.get("competition_risk_level") in {"competitive", "saturated_unless_differentiated"}:
        steps.append("Review differentiation against nearby CPR/BLS providers before advancing.")
    if _score_band(scores.get("market_demand_score")) in {"weak", "limited"}:
        steps.append("Run a small field demand test before committing resources.")
    if _score_band(scores.get("validation_evidence_score")) in {"unknown", "weak", "limited"}:
        steps.append("Add non-Places validation evidence or manual review before ranking higher.")
    if not steps:
        steps.append("Move to site screening with manual review; this is decision support, not approval.")
    return steps


def _risk_flags(row: Dict[str, Any], scores: Dict[str, Any]) -> List[str]:
    existing = row.get("risk_flags") or []
    if isinstance(existing, str):
        existing = [existing]
    flags = [str(flag) for flag in existing if flag]

    def add(flag: str) -> None:
        if flag and flag not in flags:
            flags.append(flag)

    if not scores.get("commercial_feasibility_confirmed"):
        status = scores.get("commercial_feasibility_status")
        if status == "unknown":
            add("commercial_feasibility_missing")
        else:
            add("commercial_feasibility_partial")
        add("site_priority_decision_capped")
    if scores.get("competition_risk_level") in {"competitive", "saturated_unless_differentiated"}:
        add("competition_saturation_risk")
    if (
        (scores.get("market_demand_score") or 0) < 55
        and (_places_support_score(row) or 0) >= 80
    ):
        add("places_signal_not_enough_without_public_demand")
    if scores.get("final_site_priority_score") is not None:
        add("v21_decision_support_only")
    return flags


def explain_site_priority(row: Dict[str, Any],
                          scores: Dict[str, Any]) -> Dict[str, Any]:
    """Return explanation text, next steps, decision label, and risk flags."""
    market_band = _score_band(scores.get("market_demand_score"))
    validation_band = _score_band(scores.get("validation_evidence_score"))
    commercial_status = scores.get("commercial_feasibility_status")
    risk = scores.get("competition_risk_level")

    parts = [
        f"Market demand is {market_band}.",
        f"Validation evidence is {validation_band}.",
    ]
    if risk == "saturated_unless_differentiated":
        parts.append(
            "Competitors prove demand exists, but saturation risk is high and differentiation is required."
        )
    elif risk == "unproven_market":
        parts.append(
            "No competitors are visible, so saturation is low but market proof is limited."
        )
    elif risk in {"competitive", "competitive_but_healthy", "low_to_moderate"}:
        parts.append(f"Competition risk is {risk.replace('_', ' ')}.")
    else:
        parts.append("Competition evidence is incomplete.")

    if commercial_status == "confirmed":
        parts.append("Commercial feasibility is manually validated enough for site screening.")
    elif commercial_status == "unknown":
        parts.append("Commercial feasibility is missing, so the decision is capped pending field validation.")
    else:
        parts.append("Commercial feasibility is partial, so rent, parking, classroom fit, and availability still need review.")

    return {
        "site_priority_decision": _decision_label(scores),
        "site_priority_explanation": " ".join(parts),
        "site_priority_next_steps": _next_steps(scores),
        "site_priority_risk_flags": _risk_flags(row, scores),
    }


def _fmt_num(value: Any) -> str:
    """Format a score for plain-language text (drops a trailing .0)."""
    number = _number(value)
    if number is None:
        return "—"
    rounded = round(number, 1)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


def _breakdown_note(key: str, scores: Dict[str, Any]) -> str:
    """Explain when a weighted component is a neutral placeholder, not real data."""
    if key == "commercial_feasibility":
        status = str(scores.get("commercial_feasibility_status") or "unknown")
        if scores.get("commercial_feasibility_score") is None:
            return (
                f"{status.replace('_', ' ').capitalize()} — "
                f"neutral {UNKNOWN_COMMERCIAL_SCORE:.0f} used until field validation."
            )
    elif key == "historical_fit":
        if scores.get("historical_allcpr_fit_score") is None:
            return f"No ALLCPR history — neutral {UNKNOWN_HISTORICAL_FIT_SCORE:.0f} used."
    return ""


def _breakdown_summary(scores: Dict[str, Any], final: Optional[float],
                       penalty: float) -> str:
    """One plain-language sentence: why the final score sits where it does."""
    if final is None:
        return (
            "Final score is withheld because the baseline market or validation "
            "inputs are missing."
        )

    validation = _number(scores.get("validation_evidence_score"))
    lead = ""
    if validation is not None and validation >= 75:
        lead = (
            f"High validation evidence ({_fmt_num(validation)}) means this ZIP has "
            "strong real-world signals, not that it is ready to open. "
        )

    reasons: List[str] = []
    if str(scores.get("commercial_feasibility_status") or "unknown") != "confirmed":
        status = str(scores.get("commercial_feasibility_status") or "unknown")
        reasons.append(
            f"commercial feasibility is {status.replace('_', ' ')} "
            f"(neutral {UNKNOWN_COMMERCIAL_SCORE:.0f} used)"
        )
    if scores.get("historical_allcpr_fit_score") is None:
        reasons.append(
            f"there is no ALLCPR history (neutral {UNKNOWN_HISTORICAL_FIT_SCORE:.0f} used)"
        )
    if penalty:
        reasons.append(f"competition saturation subtracts {_fmt_num(penalty)}")

    if not reasons:
        return f"{lead}The final score is {_fmt_num(final)}.".strip()
    if len(reasons) == 1:
        joined = reasons[0]
    else:
        joined = ", ".join(reasons[:-1]) + f", and {reasons[-1]}"
    return f"{lead}The final score is held to {_fmt_num(final)} because {joined}."


def build_site_priority_breakdown(scores: Dict[str, Any]) -> Dict[str, Any]:
    """Show how the weighted components and penalty produce the final score.

    Answers the common operator question "why is the final score lower than the
    validation-evidence score?" by exposing each weighted contribution and the
    competition penalty as plain numbers that add up to the final score.
    """
    components: List[Dict[str, Any]] = []
    subtotal = 0.0
    for key, label, field, weight in SITE_PRIORITY_COMPONENTS:
        value = _number(scores.get(field))
        weighted = None if value is None else round(value * weight, 1)
        if value is not None:
            subtotal += value * weight
        components.append({
            "key": key,
            "label": label,
            "value": value,
            "weight": weight,
            "weight_pct": round(weight * 100),
            "weighted_points": weighted,
            "note": _breakdown_note(key, scores),
        })

    penalty = _number(scores.get("competition_saturation_penalty")) or 0.0
    final = scores.get("final_site_priority_score")
    return {
        "components": components,
        "subtotal": _round_score(subtotal),
        "competition_saturation_penalty": round(penalty, 1),
        "final_site_priority_score": final,
        "capped": scores.get("site_priority_score_status") != "commercial_confirmed",
        "summary": _breakdown_summary(scores, final, penalty),
    }


def _course_historical_score(row: Dict[str, Any], course: str) -> Optional[float]:
    score_keys = {
        "aha_bls": ("proven_aha_bls_score", "aha_bls_score"),
        "arc_bls": ("proven_arc_bls_score", "arc_bls_score"),
        "arc_cpr": ("proven_arc_cpr_score", "arc_cpr_score"),
    }
    student_keys = {
        "aha_bls": ("aha_bls_students", "proven_aha_bls_students"),
        "arc_bls": ("arc_bls_students", "proven_arc_bls_students"),
        "arc_cpr": ("arc_cpr_students", "proven_arc_cpr_students"),
    }
    direct = _first_number(row, *score_keys.get(course, ()))
    if direct is not None:
        return _round_score(direct)

    mix = row.get("historical_course_mix")
    if isinstance(mix, dict):
        share = _number(mix.get(course))
        if share is not None:
            return _round_score(100.0 * share)

    students = _first_number(row, *student_keys.get(course, ()))
    if students is not None:
        return _log_count_score(students, 120.0)
    return None


def _historical_course_phrase(row: Dict[str, Any], course: str) -> str:
    label = COURSE_LABELS.get(course, course)
    historical = _course_historical_score(row, course)
    if historical is None:
        return f"Historical {label} data is not provided for this ZIP."
    band = _display_band(historical)
    best = str(row.get("best_historical_course") or "").lower()
    if label.lower() in best or course.replace("_", " ") in best:
        return f"Historical data points to {label} as the strongest recorded course type here."
    return f"Historical {label} evidence is {band}."


def _healthcare_anchor_score(row: Dict[str, Any]) -> Optional[float]:
    healthcare = _score_count_density(
        _first_number(row, "healthcare_facility_count", "healthcare_poi_count",
                      "medical_office_count", "clinic_provider_count"),
        _first_number(row, "healthcare_facility_density"),
        count_cap=60.0,
        density_cap=15.0,
    )
    anchors = _weighted_score((
        (_log_count_score(row.get("hospital_count"), 10.0), 0.55),
        (_log_count_score(row.get("urgent_care_count"), 10.0), 0.45),
    ))
    nursing = _log_count_score(row.get("nursing_school_count"), 12.0)
    return _weighted_score(((healthcare, 0.55), (anchors, 0.30), (nursing, 0.15)))


def _community_training_score(row: Dict[str, Any]) -> Optional[float]:
    community = _score_count_density(
        _first_number(row, "community_facility_count", "school_count",
                      "childcare_count"),
        _first_number(row, "community_facility_density"),
        count_cap=70.0,
        density_cap=25.0,
    )
    training = _score_count_density(
        _first_number(row, "training_school_count",
                      "health_program_school_count"),
        _first_number(row, "training_school_density"),
        count_cap=45.0,
        density_cap=8.0,
    )
    population = _weighted_score((
        (_linear_score(_first_number(row, "population"), 5_000.0, 60_000.0), 0.50),
        (_linear_score(_first_number(row, "population_density"), 500.0, 10_000.0), 0.50),
    ))
    return _weighted_score(((community, 0.35), (training, 0.30), (population, 0.35)))


def _course_fit_score(row: Dict[str, Any], course: str,
                      base_scores: Dict[str, Any]) -> Optional[float]:
    healthcare = _healthcare_anchor_score(row)
    community = _community_training_score(row)
    bls = _first_number(row, "bls_demand", "aha_bls", "arc_bls")
    cpr = _first_number(row, "cpr_demand", "arc_cpr")
    workforce = _weighted_score((
        (bls, 0.75),
        (_linear_score(row.get("healthcare_employment_share"), 0.005, 0.15), 0.25),
    ))
    historical = _course_historical_score(row, course)

    if course == "aha_bls":
        return _weighted_score((
            (bls, 0.30),
            (healthcare, 0.30),
            (workforce, 0.20),
            (historical, 0.20),
        ))
    if course == "arc_bls":
        return _weighted_score((
            (bls, 0.30),
            (healthcare, 0.25),
            (community, 0.15),
            (workforce, 0.15),
            (historical, 0.15),
        ))
    if course == "arc_cpr":
        return _weighted_score((
            (cpr, 0.35),
            (community, 0.30),
            (historical, 0.20),
            (healthcare, 0.10),
            (workforce, 0.05),
        ))

    market = base_scores.get("market_demand_score")
    return _weighted_score((
        (market, 0.45),
        (bls, 0.20),
        (cpr, 0.20),
        (historical if historical is not None else calculate_historical_allcpr_fit_score(row), 0.15),
    ))


def _course_fit_label(course: str, fit: Optional[float],
                      scores: Dict[str, Any]) -> str:
    if fit is None:
        return "Insufficient data"
    risk = scores.get("competition_risk_level")
    commercial_confirmed = bool(scores.get("commercial_feasibility_confirmed"))
    if fit >= 70 and risk == "saturated_unless_differentiated":
        return f"Good {COURSE_LABELS.get(course, course)} fit, but saturated"
    if fit >= 72 and commercial_confirmed:
        return "Strong fit"
    if fit >= 62:
        return "Good fit, needs validation"
    if fit >= 45:
        return "Moderate fit"
    return "Weak fit"


def _course_blockers(course: str, fit: Optional[float],
                     scores: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    risk = scores.get("competition_risk_level")
    if risk in {"competitive", "saturated_unless_differentiated"}:
        blockers.append("Competition saturation")
    elif risk == "unproven_market":
        blockers.append("Market proof")

    status = scores.get("commercial_feasibility_status")
    if not scores.get("commercial_feasibility_confirmed"):
        if status == "unknown":
            blockers.append("Commercial validation")
        else:
            blockers.append("Commercial validation")

    if fit is None:
        blockers.append("Course-specific data")
    elif fit < 45:
        blockers.append(f"{COURSE_LABELS.get(course, course)} demand")

    return list(dict.fromkeys(blockers))


def _course_decision_status(fit: Optional[float], scores: Dict[str, Any],
                            blockers: Sequence[str]) -> Tuple[str, str]:
    risk = scores.get("competition_risk_level")
    commercial_confirmed = bool(scores.get("commercial_feasibility_confirmed"))
    validation = scores.get("validation_evidence_score")

    if fit is None:
        return "Insufficient data", "Insufficient data"
    if fit < 40:
        return "Low priority", "Do not proceed yet"
    if not commercial_confirmed:
        if fit >= 60 and risk == "saturated_unless_differentiated":
            return "Good demand, but saturated", "Needs commercial validation"
        if fit >= 55 or (validation or 0) >= 70:
            return "Good demand, validate commercial fit", "Needs commercial validation"
        return "Needs field test", "Needs field test"
    if risk == "saturated_unless_differentiated":
        return "Good demand, but saturated", "High competition risk"
    if fit >= 70 and (validation or 0) >= 65:
        return "Ready for field assessment", "Ready for screening"
    if fit >= 55:
        return "Needs field test", "Needs field test"
    return "Watchlist", "Watchlist"


def _course_next_action(course: str, decision: str, scores: Dict[str, Any]) -> str:
    risk = scores.get("competition_risk_level")
    commercial_confirmed = bool(scores.get("commercial_feasibility_confirmed"))
    label = COURSE_LABELS.get(course, course)
    if not commercial_confirmed and risk == "saturated_unless_differentiated":
        return "Validate rent, parking, classroom fit, access, and differentiation strategy before opening decision."
    if not commercial_confirmed:
        return "Validate rent, parking, classroom fit, and access before any opening decision."
    if risk == "saturated_unless_differentiated":
        return "Validate differentiation, pricing, scheduling, and nearby demand before opening."
    if course == "aha_bls":
        return "Check nearby healthcare employer partnerships and AHA instructor availability."
    if course == "arc_bls":
        return "Check workplace and healthcare partner demand, then validate instructor and classroom fit."
    if course == "arc_cpr":
        return "Validate community demand, pricing, parking/classroom fit, and local differentiation."
    return f"Use {label} tabs for class planning, then validate commercial fit before opening."


def _course_why_bullets(row: Dict[str, Any], course: str,
                        scores: Dict[str, Any]) -> List[str]:
    risk_label = readable_competition_risk(scores.get("competition_risk_level"))
    commercial_status = str(scores.get("commercial_feasibility_status") or "unknown")
    healthcare = _healthcare_anchor_score(row)
    community = _community_training_score(row)
    bls = _first_number(row, "bls_demand", "aha_bls", "arc_bls")
    cpr = _first_number(row, "cpr_demand", "arc_cpr")
    healthcare_share = _linear_score(row.get("healthcare_employment_share"), 0.005, 0.15)

    if course == "aha_bls":
        bullets = [
            f"Healthcare/BLS workforce demand is {_display_band(bls)}.",
            f"Healthcare facilities and anchors are {_display_band(healthcare)}.",
            f"Healthcare employment share is {_display_band(healthcare_share)}, so employer demand should be validated.",
            _historical_course_phrase(row, course),
        ]
    elif course == "arc_bls":
        bullets = [
            f"Healthcare and workplace demand is {_display_band(bls)}.",
            f"Healthcare anchors are {_display_band(healthcare)} and general training signals are {_display_band(community)}.",
            _historical_course_phrase(row, course),
        ]
    elif course == "arc_cpr":
        bullets = [
            f"Community CPR demand is {_display_band(cpr)}.",
            f"Training, school, childcare, and community signals are {_display_band(community)}.",
            _historical_course_phrase(row, course),
        ]
    else:
        bullets = [
            "Overall is a blended market view. Use course-specific tabs for class planning.",
            f"Blended market demand is {_display_band(scores.get('market_demand_score'))}.",
            f"Validation evidence is {_display_band(scores.get('validation_evidence_score'))}.",
        ]

    if scores.get("competition_risk_level") in {"competitive", "saturated_unless_differentiated"}:
        bullets.append(f"Competition is {risk_label.lower()}, so differentiation is required.")
    elif scores.get("competition_risk_level") == "unproven_market":
        bullets.append("No visible competitors means saturation is low, but demand proof is limited.")
    else:
        bullets.append(f"Competition profile: {risk_label}.")

    if commercial_status == "confirmed":
        bullets.append("Commercial feasibility has enough confirmation for field assessment.")
    else:
        bullets.append("Commercial feasibility is not confirmed, so do not approve opening yet.")
    return bullets[:5]


def _operator_summary(course: str, fit: Optional[float], decision: str,
                      scores: Dict[str, Any]) -> str:
    course_label = COURSE_LABELS.get(course, course)
    fit_band = _display_band(fit)
    validation_band = _display_band(scores.get("validation_evidence_score"))
    risk = scores.get("competition_risk_level")
    commercial_confirmed = bool(scores.get("commercial_feasibility_confirmed"))

    if course == "overall":
        lead = "This blended view is useful for market discovery, but course tabs should guide class planning."
    else:
        lead = f"For {course_label}, this ZIP shows {fit_band} course fit with {validation_band} validation evidence."

    if risk == "saturated_unless_differentiated":
        lead += " Demand appears proven, but high competition means ALLCPR needs a clear differentiation strategy."
    elif risk == "competitive":
        lead += " The market is competitive, so pricing, schedule, and partner strategy matter."

    if not commercial_confirmed:
        lead += " Commercial validation is still required before any opening decision."
    elif decision == "Ready for field assessment":
        lead += " Commercial evidence is confirmed enough to move into field assessment."
    return lead


def calculate_course_priority_profile(
    row: Dict[str, Any],
    course: str,
    scores: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the operator-facing course-specific interpretation for a ZIP."""
    course = course if course in COURSE_LABELS else "overall"
    scores = scores or calculate_final_site_priority_score(row)
    fit = _course_fit_score(row, course, scores)
    blockers = _course_blockers(course, fit, scores)
    decision, status = _course_decision_status(fit, scores, blockers)
    next_action = _course_next_action(course, decision, scores)
    validation = scores.get("validation_evidence_score")
    base = _weighted_score((
        (fit, 0.45),
        (validation, 0.25),
        (scores.get("commercial_feasibility_score_used"), 0.20),
        (
            _course_historical_score(row, course)
            if course != "overall"
            else scores.get("historical_allcpr_fit_score_used"),
            0.10,
        ),
    ))
    course_priority = _round_score(
        None if base is None else base - (scores.get("competition_saturation_penalty") or 0.0)
    )
    label = _course_fit_label(course, fit, scores)
    why = _course_why_bullets(row, course, scores)
    reason = " ".join(why)

    return {
        "selected_course_type": course,
        "selected_course_label": COURSE_LABELS[course],
        "course_fit_score": fit,
        "course_site_priority_score": course_priority,
        "course_fit_label": label,
        "course_fit_reason": reason,
        "course_specific_next_action": next_action,
        "course_specific_blockers": blockers,
        "course_why_bullets": why,
        "operator_decision": decision,
        "operator_status": status,
        "operator_summary": _operator_summary(course, fit, decision, scores),
        "operator_confidence": _confidence_label(validation),
        "competition_risk_label": readable_competition_risk(scores.get("competition_risk_level")),
    }


def build_course_priority_profiles(
    row: Dict[str, Any],
    scores: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build profiles for every dashboard course tab."""
    scores = scores or calculate_final_site_priority_score(row)
    return {
        course: calculate_course_priority_profile(row, course, scores)
        for course in COURSE_LABELS
    }


def calculate_final_site_priority_score(row: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate all v2.1 site-priority fields for a ZIP row."""
    market = calculate_market_demand_score(row)
    validation = calculate_validation_evidence_score(row)
    commercial = calculate_commercial_feasibility_score(row)
    competition = calculate_competition_profile(row)
    historical = calculate_historical_allcpr_fit_score(row)

    if _critical_data_missing(row, market, validation):
        final = None
    else:
        base = _weighted_score((
            (market, 0.45),
            (validation, 0.25),
            (commercial["commercial_feasibility_score_used"], 0.20),
            (
                historical if historical is not None else UNKNOWN_HISTORICAL_FIT_SCORE,
                0.10,
            ),
        ))
        penalty = competition.get("competition_saturation_penalty") or 0.0
        final = _round_score(None if base is None else base - penalty)

    scores: Dict[str, Any] = {
        "score_formula_version": SCORE_FORMULA_VERSION,
        "market_demand_score": market,
        "validation_evidence_score": validation,
        "historical_allcpr_fit_score": historical,
        "historical_allcpr_fit_score_used": (
            historical if historical is not None else UNKNOWN_HISTORICAL_FIT_SCORE
        ),
        "final_site_priority_score": final,
        "site_priority_score_status": (
            "insufficient_data" if final is None else
            "provisional" if not commercial["commercial_feasibility_confirmed"] else
            "commercial_confirmed"
        ),
        **commercial,
        **competition,
    }
    scores["competition_risk_label"] = readable_competition_risk(
        scores.get("competition_risk_level")
    )
    scores.update(explain_site_priority(row, scores))
    scores["site_priority_score_breakdown"] = build_site_priority_breakdown(scores)
    profiles = build_course_priority_profiles(row, scores)
    scores["course_priority_profiles"] = profiles
    scores.update({
        "selected_course_type": profiles["overall"]["selected_course_type"],
        "course_fit_score": profiles["overall"]["course_fit_score"],
        "course_fit_label": profiles["overall"]["course_fit_label"],
        "course_fit_reason": profiles["overall"]["course_fit_reason"],
        "course_specific_next_action": profiles["overall"]["course_specific_next_action"],
        "course_specific_blockers": profiles["overall"]["course_specific_blockers"],
    })
    return scores


def annotate_site_priority_scores(row: Dict[str, Any],
                                  *,
                                  in_place: bool = False) -> Dict[str, Any]:
    """Attach flattened v2.1 fields to a ZIP row, preserving old fields."""
    target = row if in_place else dict(row)
    target.update(calculate_final_site_priority_score(target))
    return target


def annotate_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return rows with v2.1 fields attached."""
    return [annotate_site_priority_scores(row) for row in rows]
