"""
National ZIP-level MODELED opportunity score (0–100).

This is the engine behind the dashboard's "Modeled national demand" layer. It
estimates how attractive a ZIP looks for a CPR/BLS training center using only
public data (Census ACS ZCTA demographics + Gazetteer density), so it can cover
*every* US ZIP — including markets where ALLCPR has never taught and therefore
has no real history.

It is deliberately kept separate from the real-history layer
(:mod:`app.scoring.zip_demand`):
  * Different inputs (public proxies, not Enrollware enrollment).
  * Different output file / endpoint / map layer.
  * Labeled an ESTIMATE everywhere — never a guaranteed enrollment prediction,
    and never blended into a single number with the historical score.

Two course "tilts" are derivable from public data and selectable in the UI:
  * ``bls_demand``  — healthcare-workforce emphasis (BLS audience).
  * ``cpr_demand``  — community / layperson emphasis (CPR audience).
  * ``overall``     — the mean of the two.
Brand (AHA vs ARC) is intentionally NOT modeled: no public dataset encodes it.

Phase 2 (enrichment) is additive: supply extra ``features`` (facility counts,
competitor gap, etc.) for selected ZIPs and the same function folds them in,
flipping ``tier`` to ``"enriched"`` — no formula rewrite, no dashboard change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.config import (
    ZIP_MODEL_BOUNDS,
    ZIP_MODELED_WEIGHTS_BLS,
    ZIP_MODELED_WEIGHTS_CPR,
)
from app.scoring.economy_score import _norm

# Public ACS/Gazetteer signals available for every ZIP (the baseline tier).
BASELINE_SIGNALS = (
    "population",
    "population_density",
    "median_household_income",
    "working_age_share",
    "employment_rate",
    "bachelors_or_higher_share",
    "healthcare_employment_share",
)
# Signals that only exist after Phase-2 enrichment of a ZIP.
ENRICHMENT_SIGNALS = (
    "healthcare_facility_density",
    "community_facility_density",
    "training_school_density",
    "competition_gap_score",
)

_SIGNAL_LABELS = {
    "population": "population",
    "population_density": "population density",
    "median_household_income": "median household income",
    "working_age_share": "working-age share",
    "employment_rate": "employment rate",
    "bachelors_or_higher_share": "bachelor's-or-higher share",
    "healthcare_employment_share": "healthcare-employment share",
    "healthcare_facility_density": "healthcare-facility density",
    "community_facility_density": "community-facility density",
    "training_school_density": "training-school density",
    "competition_gap_score": "competition gap",
}


def _tilt_score(
    features: Dict[str, Any], weights: Dict[str, float]
) -> tuple[Optional[float], List[str]]:
    """Weighted, renormalized 0–100 score over the signals actually present.

    Missing signals (``None`` / absent) drop out and the remaining weights
    renormalize — so a baseline-only ZIP scores on public signals alone, and an
    enriched ZIP simply has more terms. Never invents a value for a gap.
    """
    weighted_sum = 0.0
    weight_used = 0.0
    used: List[str] = []
    for field, weight in weights.items():
        bounds = ZIP_MODEL_BOUNDS.get(field)
        if not bounds:
            continue
        norm = _norm(features.get(field), bounds[0], bounds[1])
        if norm is None:
            continue
        weighted_sum += norm * weight
        weight_used += weight
        used.append(field)
    if weight_used <= 0:
        return None, []
    return round(100.0 * weighted_sum / weight_used, 1), used


def signal_weight_breakdown(
    features: Dict[str, Any], weights: Dict[str, float]
) -> Dict[str, Any]:
    """Per-signal debug view of one tilt, exposing the renormalization denominator.

    Mirrors :func:`_tilt_score` exactly (same bounds, same drop-on-missing rule)
    so the debug table and the real score can never diverge. For every weighted
    signal it reports the raw value, bounds, normalized 0..1 value, weight, and
    contribution (``normalized * weight``). ``weight_used`` is the sum of weights
    for signals actually present — i.e. the denominator the weighted sum is
    divided by. With a full baseline that is ~0.80; it only reaches 1.00 once all
    enhanced (enrichment) signals for the tilt are present too.
    """
    rows: List[Dict[str, Any]] = []
    weight_used = 0.0
    weighted_sum = 0.0
    for field, weight in weights.items():
        bounds = ZIP_MODEL_BOUNDS.get(field)
        norm = _norm(features.get(field), bounds[0], bounds[1]) if bounds else None
        present = norm is not None
        contribution = norm * weight if present else 0.0
        if present:
            weight_used += weight
            weighted_sum += contribution
        rows.append({
            "field": field,
            "label": _SIGNAL_LABELS.get(field, field),
            "raw_value": features.get(field),
            "bounds": list(bounds) if bounds else None,
            "normalized": round(norm, 4) if present else None,
            "weight": weight,
            "contribution": round(contribution, 4) if present else 0.0,
            "present": present,
            "enhanced": field in ENRICHMENT_SIGNALS,
        })
    score = round(100.0 * weighted_sum / weight_used, 1) if weight_used > 0 else None
    return {
        "rows": rows,
        "weight_used": round(weight_used, 6),
        "weighted_sum": round(weighted_sum, 6),
        "score": score,
    }


def _recommendation(overall: Optional[float]) -> str:
    """Automated opportunity recommendation, never a human-triage fallback."""
    if overall is None:
        return "Insufficient data"
    if overall >= 70:
        return "Strong opportunity"
    if overall >= 55:
        return "Promising opportunity"
    if overall >= 40:
        return "Watchlist"
    return "Low priority"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _presence(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "1", "available", "validated"}:
            return True
        if text in {"false", "no", "n", "0", "none", "unavailable"}:
            return False
    return None


def _cap_score(value: Any, cap: float) -> Optional[float]:
    n = _number(value)
    if n is None:
        return None
    return max(0.0, min(100.0, 100.0 * n / cap))


def _competitor_gap_score(features: Dict[str, Any]) -> Optional[float]:
    existing = _number(features.get("competition_gap_score"))
    if existing is not None:
        return max(0.0, min(100.0, existing))
    competitors = _number(features.get("competitor_count"))
    if competitors is None:
        return None
    if competitors <= 2:
        return 100.0
    if competitors <= 5:
        return 75.0
    if competitors <= 10:
        return 45.0
    if competitors <= 20:
        return 20.0
    return 5.0


def _commercial_validation_score(features: Dict[str, Any]) -> Optional[float]:
    parts: List[float] = []
    available = _presence(features.get("commercial_space_available"))
    if available is not None:
        parts.append(80.0 if available else 20.0)
    ready = _presence(features.get("commercial_ready"))
    if ready is not None:
        parts.append(100.0 if ready else 35.0)
    parking = _number(features.get("parking_score"))
    if parking is not None:
        parts.append(max(0.0, min(100.0, parking)))
    proxy = _number(features.get("parking_proxy_score"))
    if proxy is not None:
        parts.append(max(0.0, min(100.0, proxy)))
    classroom = _presence(features.get("classroom_fit"))
    if classroom is not None:
        parts.append(90.0 if classroom else 25.0)
    return round(sum(parts) / len(parts), 1) if parts else None


def _historical_proximity_score(features: Dict[str, Any]) -> Optional[float]:
    direct = _number(features.get("allcpr_historical_proximity_score"))
    if direct is not None:
        return max(0.0, min(100.0, direct))
    miles = _number(features.get("nearest_allcpr_history_miles"))
    if miles is None:
        return None
    if miles <= 3:
        return 100.0
    if miles <= 8:
        return 75.0
    if miles <= 15:
        return 45.0
    return 15.0


def automated_validation(features: Dict[str, Any],
                         overall: Optional[float],
                         confidence: str) -> Dict[str, Any]:
    """Explainable validation/override layer for modeled ZIP recommendations.

    Missing validation signals lower confidence; they are not treated as bad
    evidence. The output is additive and keeps the original score fields intact.
    """
    density_score = None
    bounds = ZIP_MODEL_BOUNDS.get("population_density")
    if bounds:
        n = _norm(features.get("population_density"), bounds[0], bounds[1])
        density_score = round(n * 100.0, 1) if n is not None else None

    healthcare_count = (
        _number(features.get("healthcare_poi_count"))
        or _number(features.get("medical_office_count"))
        or _number(features.get("healthcare_facility_count"))
    )
    healthcare_density = _number(features.get("healthcare_facility_density"))
    if healthcare_count is None and healthcare_density is not None:
        healthcare_count = healthcare_density

    signal_parts = {
        "hospital_count": _cap_score(features.get("hospital_count"), 3),
        "urgent_care_count": _cap_score(features.get("urgent_care_count"), 5),
        "nursing_school_count": _cap_score(features.get("nursing_school_count"), 4),
        "healthcare_poi_count": _cap_score(healthcare_count, 25),
        "community_facility_count": _cap_score(
            features.get("community_facility_count"), 20),
        "competitor_gap": _competitor_gap_score(features),
        "population_density": density_score,
        "commercial_readiness": _commercial_validation_score(features),
        "allcpr_historical_proximity": _historical_proximity_score(features),
    }
    weights = {
        "hospital_count": 1.2,
        "urgent_care_count": 1.0,
        "nursing_school_count": 1.0,
        "healthcare_poi_count": 1.4,
        "community_facility_count": 0.9,
        "competitor_gap": 1.0,
        "population_density": 0.9,
        "commercial_readiness": 0.8,
        "allcpr_historical_proximity": 1.0,
    }
    present = {k: v for k, v in signal_parts.items() if v is not None}
    validation_score = None
    if present:
        total_weight = sum(weights[k] for k in present)
        validation_score = round(
            sum(present[k] * weights[k] for k in present) / total_weight, 1)

    missing = [k for k, v in signal_parts.items() if v is None]
    missing_core = [
        k for k in (
            "hospital_count", "urgent_care_count", "nursing_school_count",
            "healthcare_poi_count", "competitor_gap", "population_density")
        if signal_parts.get(k) is None
    ]
    overall_num = _number(overall)
    if confidence == "missing" or overall_num is None:
        tier = "Insufficient data"
        confidence_reason = (
            "Insufficient data — not rejected. Some public data is missing or "
            "incomplete, so the model lowers confidence instead of treating the "
            "ZIP as poor. Additional enrichment signals can automatically "
            "upgrade this area."
        )
        recommendation_reason = confidence_reason
        upgrade_reason = ""
        downgrade_reason = "Core modeled inputs are incomplete."
    elif confidence != "ok" and (len(present) < 3 or len(missing_core) >= 4):
        tier = "Insufficient data"
        confidence_reason = (
            "Important validation signals are missing, so confidence is low. "
            "Missing data is not treated as negative evidence."
        )
        recommendation_reason = confidence_reason
        upgrade_reason = ""
        downgrade_reason = "Too few automated validation signals are available."
    else:
        score = overall_num or 0.0
        vscore = validation_score or 0.0
        confidence_reason = (
            "Automated validation used available demographic, healthcare, "
            "competition, community, commercial, and proximity signals; missing "
            "signals reduce confidence rather than opportunity."
        )
        if score >= 70 and vscore >= 65:
            tier = "Strong opportunity"
            recommendation_reason = (
                "Strong opportunity — baseline demand and automated validation "
                "signals are both strong."
            )
            upgrade_reason = "Baseline and enrichment signals agree."
            downgrade_reason = ""
        elif score >= 55 and vscore >= 65:
            tier = "Validation-supported opportunity"
            recommendation_reason = (
                "Validation-supported opportunity — baseline demand is moderate "
                "and enrichment signals provide strong real-world support."
            )
            upgrade_reason = "Strong healthcare/community/commercial validation."
            downgrade_reason = ""
        elif score >= 55:
            tier = "Promising opportunity"
            recommendation_reason = (
                "Promising opportunity — baseline demand is solid; automated "
                "validation is moderate or incomplete."
            )
            upgrade_reason = ""
            downgrade_reason = ""
        elif score >= 40 or vscore >= 65:
            tier = "Watchlist"
            recommendation_reason = (
                "Watchlist — baseline demand is limited or mixed, but automated "
                "signals may support future validation."
            )
            upgrade_reason = (
                "Strong enrichment signals partially offset a low baseline."
                if vscore >= 65 and score < 55 else ""
            )
            downgrade_reason = ""
        else:
            tier = "Low priority"
            recommendation_reason = (
                "Low priority — automated signals are currently weak. This ZIP "
                "does not show enough demographic, healthcare, or real-world "
                "validation signals to rank highly right now."
            )
            upgrade_reason = ""
            downgrade_reason = "Baseline and automated validation signals are weak."

    return {
        "validation_score": validation_score,
        "validation_tier": tier,
        "confidence_reason": confidence_reason,
        "recommendation_reason": recommendation_reason,
        "upgrade_reason": upgrade_reason,
        "downgrade_reason": downgrade_reason,
        "validation_signal_count": len(present),
        "validation_missing_signals": missing,
    }


def _strong_validation(validation: Dict[str, Any]) -> bool:
    score = _number(validation.get("validation_score"))
    return score is not None and score >= 70.0


def _rural_market_caps(
    features: Dict[str, Any],
    *,
    overall: Optional[float],
    bls: Optional[float],
    cpr: Optional[float],
    validation: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply rural/low-density caps unless real-world validation is strong."""
    density = _number(features.get("population_density"))
    population = _number(features.get("population"))
    healthcare_share = _number(features.get("healthcare_employment_share"))
    api_priority = str(features.get("api_priority") or "").strip().lower()
    api_reason = str(features.get("api_filter_reason") or "").strip().lower()
    has_strong_validation = _strong_validation(validation)

    capped_overall = overall
    capped_bls = bls
    capped_cpr = cpr
    caps: List[Dict[str, Any]] = []
    weaknesses: List[str] = []

    def apply_overall_cap(cap: float, reason: str) -> None:
        nonlocal capped_overall
        if capped_overall is not None and capped_overall > cap:
            capped_overall = cap
            caps.append({"field": "overall", "cap": cap, "reason": reason})

    def apply_bls_cap(cap: float, reason: str) -> None:
        nonlocal capped_bls
        if capped_bls is not None and capped_bls > cap:
            capped_bls = cap
            caps.append({"field": "bls_demand", "cap": cap, "reason": reason})

    if density is not None and density < 50:
        weaknesses.append("population density: very low rural market")
        if not has_strong_validation:
            apply_overall_cap(25.0, "rural_low_density_cap")
    if (
        population is not None and population < 10_000
        and density is not None and density < 100
    ):
        weaknesses.append("population: small low-density market")
        if not has_strong_validation:
            apply_overall_cap(25.0, "small_low_density_market_cap")
    if (
        healthcare_share is not None and healthcare_share < 0.01
        and density is not None and density < 100
    ):
        weaknesses.append("healthcare-employment share: weak rural BLS signal")
        if not has_strong_validation:
            apply_bls_cap(25.0, "rural_weak_healthcare_workforce_cap")
    if api_priority == "exclude" and "low" in api_reason and "dens" in api_reason:
        weaknesses.append("API candidate gate: excluded for low density")
        if not has_strong_validation:
            apply_overall_cap(25.0, "api_low_density_exclusion_cap")

    if capped_bls is not None or capped_cpr is not None:
        parts = [v for v in (capped_bls, capped_cpr) if v is not None]
        capped_overall = (
            round(sum(parts) / len(parts), 1)
            if parts and capped_overall is not None
            else capped_overall
        )
        if caps and any(c["field"] == "overall" for c in caps):
            tightest = min(c["cap"] for c in caps if c["field"] == "overall")
            if capped_overall is not None and capped_overall > tightest:
                capped_overall = tightest

    return {
        "overall": capped_overall,
        "bls_demand": capped_bls,
        "cpr_demand": capped_cpr,
        "final_cap_applied": bool(caps),
        "cap_details": caps,
        "cap_reason": caps[0]["reason"] if caps else "",
        "rural_cap_weaknesses": weaknesses,
        "validation_override_applied": bool(caps) is False and has_strong_validation,
    }


def _signal_rows(features: Dict[str, Any], used: Sequence[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for field in used:
        bounds = ZIP_MODEL_BOUNDS.get(field)
        norm = _norm(features.get(field), bounds[0], bounds[1]) if bounds else None
        if norm is None:
            continue
        label = _SIGNAL_LABELS.get(field, field)
        strength = "strong" if norm >= 0.66 else "moderate" if norm >= 0.33 else "weak"
        rows.append({
            "field": field,
            "label": label,
            "normalized": round(norm * 100.0, 1),
            "strength": strength,
        })
    return rows


def _human_list(items: Sequence[str], *, fallback: str = "limited public signals") -> str:
    parts = [str(i) for i in items if i]
    if not parts:
        return fallback
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _score_drivers_and_weaknesses(
    signal_rows: Sequence[Dict[str, Any]],
) -> tuple[List[str], List[str]]:
    strongest = sorted(signal_rows, key=lambda r: r["normalized"], reverse=True)
    drivers = [
        f"{row['label']}: {row['strength']}"
        for row in strongest
        if row["normalized"] >= 55
    ][:5]
    weakest = sorted(signal_rows, key=lambda r: r["normalized"])
    weaknesses = [
        f"{row['label']}: {row['strength']}"
        for row in weakest
        if row["normalized"] < 45
    ][:5]
    return drivers, weaknesses


def _risk_flags(features: Dict[str, Any], confidence: str,
                overall: Optional[float]) -> List[str]:
    flags = [
        "modeled_only",
        "requires_field_test",
        "requires_commercial_validation",
        "heatmap_not_boundary_exact",
    ]
    if confidence != "ok":
        flags.append("partial_public_data")
    population = _number(features.get("population"))
    density = _number(features.get("population_density"))
    if population is None or population < 5000:
        flags.append("low_population")
    if density is None or density < 300:
        flags.append("low_density")
    bounds = ZIP_MODEL_BOUNDS.get("healthcare_employment_share")
    healthcare_norm = (
        _norm(features.get("healthcare_employment_share"), bounds[0], bounds[1])
        if bounds else None
    )
    if healthcare_norm is None or healthcare_norm < 0.33:
        flags.append("weak_healthcare_signal")
    if overall is None:
        flags.append("partial_public_data")
    return flags


def _plain_english_summary(
    overall: Optional[float],
    drivers: Sequence[str],
    weaknesses: Sequence[str],
    cap_reason: str = "",
) -> str:
    if overall is None:
        return (
            "Insufficient data — not rejected. Some public data is missing or "
            "incomplete, so confidence is lowered instead of treating the ZIP as "
            "poor."
        )
    driver_text = _human_list([d.split(":")[0] for d in drivers])
    weakness_text = _human_list([w.split(":")[0] for w in weaknesses],
                                fallback="no major public-data weakness")
    if cap_reason:
        return (
            "Low priority — rural/low-density market. This ZIP has limited "
            "local population density, weak healthcare-workforce signal, and no "
            "validated ALLCPR or Places demand. Higher income alone is not "
            "enough to support expansion without stronger real-world validation."
        )
    if overall >= 70:
        return (
            f"Strong modeled opportunity because {driver_text} look favorable. "
            f"Still validate locally; this is a public-data estimate."
        )
    if overall >= 55:
        return (
            f"Promising modeled opportunity because {driver_text} support demand. "
            f"Watch {weakness_text} before committing."
        )
    if overall >= 40:
        return (
            f"Mixed modeled opportunity: {driver_text} help the case, while "
            f"{weakness_text} limit confidence."
        )
    return (
        f"Lower modeled priority because {weakness_text} are weak or incomplete. "
        "Automated validation can upgrade this ZIP if stronger healthcare, "
        "community, competition, commercial, or proximity signals are added."
    )


def _recommended_next_action(
    overall: Optional[float],
    bls: Optional[float],
    cpr: Optional[float],
    confidence: str,
) -> str:
    if overall is None:
        return "Insufficient data — add enrichment signals before ranking."
    if confidence != "ok":
        return "Insufficient data — add enrichment signals before ranking."
    bls_value = bls or 0.0
    cpr_value = cpr or 0.0
    if overall >= 55 and bls_value >= cpr_value + 7:
        return "Automated validation supports BLS-first follow-up."
    if overall >= 55 and cpr_value >= bls_value + 7:
        return "Automated validation supports ARC CPR / First Aid follow-up."
    if overall >= 55:
        return "Automated validation supports next-step commercial screening."
    if overall >= 40:
        return "Watchlist pending stronger automated enrichment signals."
    return "Low priority unless automated enrichment signals improve."


def compute_zip_modeled_opportunity(features: Dict[str, Any]) -> Dict[str, Any]:
    """Score one ZIP from public (and optionally enriched) signals.

    ``features`` keys are any of ``BASELINE_SIGNALS`` + ``ENRICHMENT_SIGNALS``.
    Returns a dict with ``overall``/``bls_demand``/``cpr_demand`` (0–100 or
    ``None`` if no signal at all), ``tier``, ``data_quality``, ``rationale``,
    and a test-first ``recommendation``.
    """
    features = features or {}

    bls, used_bls = _tilt_score(features, ZIP_MODELED_WEIGHTS_BLS)
    cpr, used_cpr = _tilt_score(features, ZIP_MODELED_WEIGHTS_CPR)

    if bls is None and cpr is None:
        overall: Optional[float] = None
    else:
        parts = [v for v in (bls, cpr) if v is not None]
        overall = round(sum(parts) / len(parts), 1)

    used = sorted(set(used_bls) | set(used_cpr))
    baseline_used = [s for s in used if s in BASELINE_SIGNALS]
    enrichment_used = [s for s in used if s in ENRICHMENT_SIGNALS]
    tier = "enriched" if enrichment_used else "baseline"

    n_baseline = len(baseline_used)
    if n_baseline >= 5:
        confidence = "ok"
    elif n_baseline >= 3:
        confidence = "partial"
    else:
        confidence = "missing"

    signal_rows = _signal_rows(features, used)
    scored_signals = sorted(
        ((row["field"], row["normalized"] / 100.0) for row in signal_rows),
        key=lambda t: t[1],
        reverse=True,
    )
    rationale = [
        f"{_SIGNAL_LABELS.get(f, f)}: {'strong' if n >= 0.66 else 'moderate' if n >= 0.33 else 'weak'}"
        for f, n in scored_signals[:4]
    ]
    drivers, weaknesses = _score_drivers_and_weaknesses(signal_rows)
    validation = automated_validation(features, overall, confidence)
    caps = _rural_market_caps(
        features,
        overall=overall,
        bls=bls,
        cpr=cpr,
        validation=validation,
    )
    overall = caps["overall"]
    bls = caps["bls_demand"]
    cpr = caps["cpr_demand"]
    if caps["rural_cap_weaknesses"]:
        for weakness in caps["rural_cap_weaknesses"]:
            if weakness not in weaknesses:
                weaknesses.insert(0, weakness)
        if caps["final_cap_applied"]:
            weaknesses.insert(0, "final cap applied: rural low-density cap")
            rationale.insert(0, "final cap applied: rural low-density cap")
    validation = automated_validation(features, overall, confidence)

    return {
        "overall": overall,
        "bls_demand": bls,
        "cpr_demand": cpr,
        "tier": tier,
        "data_quality": {
            "used": used,
            "baseline_signal_count": n_baseline,
            "enrichment_present": bool(enrichment_used),
            "confidence": confidence,
        },
        "rationale": rationale,
        "recommendation": validation["validation_tier"] or _recommendation(overall),
        "score_drivers": drivers,
        "score_weaknesses": weaknesses,
        "plain_english_summary": _plain_english_summary(
            overall, drivers, weaknesses, caps["cap_reason"]
        ),
        "recommended_next_action": _recommended_next_action(
            overall, bls, cpr, confidence
        ),
        "risk_flags": _risk_flags(features, confidence, overall),
        "final_cap_applied": caps["final_cap_applied"],
        "cap_reason": caps["cap_reason"],
        "cap_details": caps["cap_details"],
        "validation_override_applied": caps["validation_override_applied"],
        **validation,
    }
