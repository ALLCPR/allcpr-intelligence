"""
Scoring orchestrator.

Produces TWO distinct numbers so the report can be honest about certainty:

- ``area_score`` (0..100): demand/market strength of the neighbourhood/zone.
  This is the weighted blend of all sub-scores (the historical "site_score").
  **Ranking, tiering and cohort normalization all key off area_score.**
- ``site_score`` (0..100 | None): suitability of a *specific* commercial space.
  Only produced when a candidate is a ``verified_commercial_listing`` (a human
  validated a real leasable space). Otherwise ``None`` → the report shows
  "Not validated" and says "promising area, not a confirmed leasing opportunity".

Also attaches: ``candidate_type``, ``commercial_validation``,
``business_feasibility``, ``competition_detail``, ``validation_flags``,
``next_actions`` and the tier's ``executive_state``.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.collectors import commercial_listings
from app.config import SCORE_WEIGHTS
from app.scoring import candidate_type as candidate_type_mod
from app.scoring.business_feasibility import compute_feasibility
from app.scoring.competition_detail import compute_competition_detail
from app.scoring.competition_pressure import compute_competition_pressure
from app.scoring.competition_score import compute_competition_gap_score
from app.scoring.confidence_score import compute_confidence_score
from app.scoring.demand_score import (
    compute_demand_score,
    compute_training_ecosystem_score,
)
from app.scoring.economy_score import (
    compute_accessibility_score,
    compute_economy_score,
)
from app.scoring.opportunity_score import compute_opportunity_score
from app.scoring.profitability import estimate_profitability
from app.scoring.job_demand_score import compute_job_demand_score
from app.scoring.recommendation_tier import compute_tier
from app.scoring.rent_score import compute_rent_score


def _historical_score(profile: Dict[str, object]) -> float:
    """Return neutral 50 when Enrollware history is absent/insufficient."""
    hist = profile.get("historical_performance")
    if not isinstance(hist, dict):
        return 50.0
    score = hist.get("score")
    if isinstance(score, (int, float)):
        return max(0.0, min(100.0, float(score)))
    return 50.0


def _safe_float(val: object, default: float = 0.0) -> float:
    """float() that never raises — malformed enricher payloads must not
    crash the scoring orchestrator."""
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _next_actions(is_validated: bool) -> List[str]:
    """Standard field-validation checklist surfaced on every candidate card."""
    listing_step = (
        "Confirm the validated listing details with the broker/landlord"
        if is_validated
        else "Check commercial listings within 0.5 mile (office/retail suites)"
    )
    return [
        "Call 3 nearby nursing schools / healthcare employers to gauge group-training interest",
        listing_step,
        "Verify parking availability and ADA access",
        "Verify zoning / permitted use for a training classroom",
        "Estimate classroom capacity (sq ft ÷ ~35 sq ft per student)",
        "Run a Google Ads + landing-page demand test before committing",
        "Compare the offering against the top 5 nearby competitors",
        "Contact a commercial broker / landlord for a real rent quote",
    ]


def _extract_growth_proxy(economy_block: Dict[str, object]) -> float | None:
    """Pull a 0..1 growth proxy from the Census working-age share, if present."""
    census = (economy_block or {}).get("census") or {}
    if not isinstance(census, dict):
        return None
    indicators = census.get("indicators") or {}
    working_age = indicators.get("working_age_share")
    if isinstance(working_age, (int, float)):
        return float(working_age)
    return None


def score_profile(profile: Dict[str, object]) -> Dict[str, object]:
    counts_5mi: Dict[str, int] = profile.get("counts_5mi") or {}  # type: ignore[assignment]
    counts_by_bucket: Dict[str, Dict[int, int]] = (
        profile.get("counts_by_bucket") or {}  # type: ignore[assignment]
    )
    comp_summary: Dict[str, object] = profile.get("competition_summary") or {}  # type: ignore[assignment]
    economy_block: Dict[str, object] = profile.get("economy") or {}  # type: ignore[assignment]

    demand = compute_demand_score(counts_5mi)
    training = compute_training_ecosystem_score(counts_5mi)
    competition = compute_competition_gap_score(demand.score, comp_summary)
    pressure = compute_competition_pressure(comp_summary, demand.score)
    economy = compute_economy_score(economy_block)
    accessibility = compute_accessibility_score(
        counts_by_bucket,
        profile.get("accessibility"),  # type: ignore[arg-type]
    )
    confidence = compute_confidence_score(profile)
    rent = compute_rent_score(economy_block)
    job_demand = compute_job_demand_score(profile.get("job_demand") or {})  # type: ignore[arg-type]
    growth_proxy = _extract_growth_proxy(economy_block)
    opportunity = compute_opportunity_score(
        demand_score_0_100=demand.score,
        training_score_0_100=training.score,
        competition_gap_score_0_100=competition.score,
        competition_summary=comp_summary,
        demand_breakdown=demand.by_category,
        training_breakdown=training.by_category,
        job_demand_score_0_100=job_demand.score,
        competition_pressure_score_0_100=pressure.competition_pressure_score,
        growth_proxy=growth_proxy,
    )
    state = str(profile.get("state") or "")
    profitability = estimate_profitability(
        opportunity_score_0_100=opportunity.score,
        demand_score_0_100=demand.score,
        training_score_0_100=training.score,
        state=state,
    )
    historical = _historical_score(profile)

    sub_scores: Dict[str, float] = {
        "demand_score": demand.score,
        "healthcare_training_ecosystem_score": training.score,
        "competition_gap_score": competition.score,
        "allcpr_opportunity_score": opportunity.score,
        "economy_score": economy.score,
        "accessibility_score": accessibility,
        "historical_performance_score": historical,
        "profitability_score": profitability.score,
        "job_certification_demand_score": job_demand.score or 0.0,
        # informational; deliberately NOT in SCORE_WEIGHTS.
        "confidence_score": confidence.score,
    }

    # ---- AREA score: market strength of the zone (historical site_score) ---- #
    area_score = round(sum(sub_scores[k] * w for k, w in SCORE_WEIGHTS.items()), 2)

    # ---- Candidate typing + commercial-listing validation ------------------- #
    override = commercial_listings.lookup_override(profile)
    ctype = candidate_type_mod.classify(profile, override=override)

    # ---- Competition detail (bands / direct split / pressure band) ---------- #
    comp_detail = compute_competition_detail(
        profile, comp_summary, pressure.competition_pressure_score
    )

    # ---- Business feasibility (space-level + unit economics) ---------------- #
    feasibility = compute_feasibility(
        override=override,
        state=state,
        accessibility_score=accessibility,
        confidence_score=confidence.score,
        competition_pressure_score=pressure.competition_pressure_score,
        area_score=area_score,
        revenue_low=profitability.revenue_low,
        revenue_high=profitability.revenue_high,
    )

    # ---- Gated SITE score: only when a real space is validated -------------- #
    site_validated = bool(ctype.is_site_candidate)
    site_score: Optional[float] = None
    site_score_status = "not_validated"
    if site_validated and feasibility.lease_readiness_score is not None:
        site_score = round(0.5 * area_score + 0.5 * feasibility.lease_readiness_score, 2)
        site_score_status = "validated"

    tier = compute_tier(
        site_score=area_score,  # ranking number (area-level)
        confidence_score=confidence.score,
        effective_saturation=competition.effective_saturation,
        competition_gap_score=competition.score,
        candidate_type=ctype.candidate_type,
        site_validated=site_validated,
    )

    # ---- ZIP-demand bounded adjustment (display-only; never moves ranking) -- #
    # Independent signal by design: NOT in SCORE_WEIGHTS, capped at +/-5 by
    # compute_score_adjustment, and ranking/tiers stay on area_score until the
    # signal is validated across enough cities (see the zip-demand spec).
    _zd_raw = profile.get("zip_demand")
    zip_demand: Dict[str, object] = _zd_raw if isinstance(_zd_raw, dict) else {}
    zd_adjustment = _safe_float(zip_demand.get("adjustment") or 0.0)
    zd_adjustment = max(-5.0, min(5.0, zd_adjustment))   # defensive re-cap
    zd_conf_mod = _safe_float(zip_demand.get("confidence_modifier") or 0.0)
    zd_conf_mod = max(-10.0, min(10.0, zd_conf_mod))
    final_score = round(max(0.0, min(100.0, area_score + zd_adjustment)), 2)
    confidence_adjusted = round(
        max(0.0, min(100.0, confidence.score + zd_conf_mod)), 2)

    # ---- Validation flags (honest chips) ------------------------------------ #
    has_override = override is not None
    validation_flags = {
        "lease_ready": site_validated,
        "commercial_listing_validated": site_validated,
        "parking_validated": bool(has_override and getattr(override, "parking_notes", "")),
        "rent_validated": bool(has_override and getattr(override, "asking_rent", None) is not None),
        "demand_validated": ctype.demand_validation_level,  # proxy | tested | confirmed
    }

    rationale: List[str] = []
    rationale.append("Demand drivers (top): " + (
        "; ".join(demand.rationale[:5]) if demand.rationale else "none found"
    ))
    rationale.append("Training ecosystem: " + (
        "; ".join(training.rationale[:4]) if training.rationale else "thin"
    ))
    rationale.extend(competition.rationale)
    if opportunity.rationale:
        rationale.append("ALLCPR opportunity: " + "; ".join(opportunity.rationale))
    hist_block = profile.get("historical_performance")
    if isinstance(hist_block, dict):
        status = hist_block.get("status")
        if status == "scored":
            rationale.append(
                f"Historical performance: ALLCPR history scores "
                f"{historical:.1f}/100 here from "
                f"{hist_block.get('total_classes', 0)} class(es)."
            )
        elif status == "insufficient_history":
            rationale.append(
                "Historical performance: matching ALLCPR history exists, but "
                "sample size is too small; scoring stays neutral."
            )
        else:
            rationale.append(
                "Historical performance: no matching ALLCPR history; scoring "
                "stays neutral."
            )
    else:
        rationale.append(
            "Historical performance: Enrollware history not loaded; scoring stays neutral."
        )
    _zd_zips = zip_demand.get("resolved_zips")
    zd_zips: List[str] = [str(z) for z in _zd_zips] if isinstance(_zd_zips, list) else []
    zd_score = zip_demand.get("zip_demand_score")
    if isinstance(zd_score, (int, float)):
        rationale.append(
            f"ZIP demand: {zip_demand.get('strength')} "
            f"({zd_score:.0f}/100) in "
            f"{', '.join(zd_zips)} "
            f"({zip_demand.get('match_basis')}); bounded score adjustment "
            f"{zd_adjustment:+.1f}, confidence {zd_conf_mod:+.1f}."
        )
    else:
        rationale.append(
            "ZIP demand: no matching ZIP-level class history; "
            "score and confidence unchanged."
        )
    if economy.rationale:
        rationale.append("Economy: " + "; ".join(economy.rationale))
    rationale.append(f"Candidate type: {ctype.label} — {ctype.reason}")
    if site_score is None:
        rationale.append(
            "This is a promising area, not a confirmed leasing opportunity "
            "(no validated commercial space)."
        )
    rationale.extend(feasibility.rationale)
    if rent.rent_score is None:
        rationale.append("Rent: commercial rent unknown (no matching override).")
    else:
        rationale.append(f"Rent: annual rent override scored {rent.rent_score:.1f}/100.")
    if job_demand.score is None:
        rationale.append("Job demand: public job-posting certification demand unknown.")
    else:
        rationale.append("Job demand: " + "; ".join(job_demand.rationale[:3]))
    rationale.extend(confidence.rationale)

    risks: List[str] = []
    if not site_validated:
        risks.append(
            "No validated commercial space — site_score withheld; treat as an "
            "area to investigate, not a lease-ready site."
        )
    if confidence.score < 50:
        risks.append("Low data confidence; recommendation is preliminary.")
    if competition.effective_saturation > 0.8:
        risks.append("Local CPR/BLS market appears saturated.")
    if comp_detail.competition_pressure_band in ("High", "Extreme"):
        risks.append(
            f"Competition pressure is {comp_detail.competition_pressure_band} — "
            f"ALLCPR needs clear differentiation to win here."
        )
    if economy.data_confidence == "missing":
        risks.append(
            "No Census economy data available for this geography "
            "— economy_score is a neutral placeholder."
        )
    elif economy.data_confidence == "partial":
        risks.append("Census economy data is partial — confidence is reduced.")
    for f in profile.get("missing_fields", []):  # type: ignore[union-attr]
        if isinstance(f, str) and f.startswith("real_estate."):
            risks.append("Commercial rent data missing (using stub).")
            break
    if job_demand.data_confidence == "unknown":
        risks.append(
            "Public job-posting certification demand is unknown; add cited "
            "job_postings.csv rows to improve B2B demand confidence."
        )
    if isinstance(hist_block, dict) and hist_block.get("status") == "scored":
        if historical < 45:
            risks.append(
                "ALLCPR historical enrollment underperforms in this area; "
                "validate demand before expanding classes here."
            )

    return {
        # Dual scores.
        "area_score": area_score,
        "site_score": site_score,                # None unless validated
        "site_score_status": site_score_status,  # validated | not_validated
        "ranking_score": area_score,             # legacy alias for ranking paths

        # ZIP demand: bounded, display-only signal (spec 2026-06-10).
        "base_score": area_score,
        "zip_demand_score": (
            float(zd_score) if isinstance(zd_score, (int, float)) else None
        ),
        "zip_demand_adjustment": zd_adjustment,
        "final_score": final_score,
        "confidence_score_adjusted": confidence_adjusted,

        "candidate_type": ctype.candidate_type,
        "candidate_type_label": ctype.label,
        "is_site_candidate": ctype.is_site_candidate,
        "demand_validation_level": ctype.demand_validation_level,

        "sub_scores": sub_scores,
        "tier": tier.tier,
        "tier_label": tier.label,
        "tier_reasons": tier.reasons,
        "executive_state": tier.executive_state,

        "commercial_validation": (override.to_dict() if override is not None else None),
        "business_feasibility": feasibility.to_dict(),
        "competition_detail": comp_detail.to_dict(),
        "validation_flags": validation_flags,
        "next_actions": _next_actions(site_validated),

        "demand_breakdown": {
            "by_category": demand.by_category,
            "rationale": demand.rationale,
        },
        "training_breakdown": {
            "by_category": training.by_category,
            "rationale": training.rationale,
        },
        "competition_breakdown": {
            "effective_saturation": competition.effective_saturation,
            "rationale": competition.rationale,
            "competition_pressure_score": pressure.competition_pressure_score,
            "dominant_provider_index": pressure.dominant_provider_index,
            "estimated_market_capacity": pressure.estimated_market_capacity,
            "demand_to_competition_ratio": pressure.demand_to_competition_ratio,
            "pressure_rationale": pressure.rationale,
        },
        "opportunity_breakdown": {
            "score": opportunity.score,
            "rationale": opportunity.rationale,
            "angles": opportunity.angles,
            "weakness_index": opportunity.weakness_index,
            "white_space_score": opportunity.white_space_score,
        },
        "profitability_estimate": {
            "students_low": profitability.students_low,
            "students_mid": profitability.students_mid,
            "students_high": profitability.students_high,
            "revenue_low": profitability.revenue_low,
            "revenue_mid": profitability.revenue_mid,
            "revenue_high": profitability.revenue_high,
            "avg_course_price": profitability.avg_course_price,
            "price_source": profitability.price_source,
            "price_sample_size": profitability.price_sample_size,
            "notes": profitability.notes,
            "confidence": profitability.confidence,
        },
        "rent": {
            "rent_score": rent.rent_score,
            "rent_data_confidence": rent.rent_data_confidence,
            "rent_source": rent.rent_source,
            "rent_notes": rent.rent_notes,
        },
        "job_demand": {
            "job_certification_demand_score": job_demand.score,
            "job_demand_data_confidence": job_demand.data_confidence,
            "active_postings_count": job_demand.active_postings_count,
            "certification_postings_count": job_demand.certification_postings_count,
            "top_employers": job_demand.top_employers,
            "rationale": job_demand.rationale,
            "notes": job_demand.notes,
        },
        "economy_breakdown": {
            "used_fields": economy.used_fields,
            "missing_fields": economy.missing_fields,
            "rationale": economy.rationale,
            "data_confidence": economy.data_confidence,
        },
        "confidence_breakdown": {
            "rationale": confidence.rationale,
            "dimensions": dict(confidence.dimensions or {}),
        },
        "rationale": rationale,
        "risks": risks,
    }
