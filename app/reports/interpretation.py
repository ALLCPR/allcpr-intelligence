"""
Deterministic report interpretation layer.

Turns the raw scoring output (site_score, sub_scores, breakdowns, risks) into
an executive-grade, decision-ready interpretation: expansion readiness, ranked
demand signals, go-to-market strategy labels, competitor interpretation,
plain-English warnings, score meters and a per-candidate quick read.

Everything here is pure and deterministic — same `(profile, scored)` in, same
interpretation out. No AI, no network, no invented data. Unknown stays unknown.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.enrichers.course_performance import build_course_performance
from app.reports.opportunity_gaps import compute_opportunity_gaps
from app.scoring.cohort_normalization import (
    cohort_means_from_ranked,
    factor_decomposition,
)
from app.scoring.course_performance_score import (
    compare_public_demand,
    score_course_performance,
)


# --------------------------------------------------------------------------- #
# Demand-signal business importance (point 5)
# --------------------------------------------------------------------------- #

# Rank 0 = most important. Used to re-order demand drivers by business value
# instead of by raw result count.
IMPORTANCE_TIERS: Dict[str, int] = {"Very high": 0, "High": 1, "Medium": 2, "Low": 3}

_CATEGORY_IMPORTANCE: Dict[str, str] = {
    # Very high
    "hospital": "Very high",
    "urgent_care": "Very high",
    "ems": "Very high",
    "fire_station": "Very high",
    "nursing_school": "Very high",
    "medical_school": "Very high",
    "cna_training": "Very high",
    "emt_training": "Very high",
    "healthcare_training": "Very high",
    # High
    "community_college": "High",
    "university": "High",
    "medical_clinic": "High",
    "dental_school": "High",
    "dental_clinic": "High",
    "senior_care": "High",
    # Medium
    "childcare_center": "Medium",
    "physical_therapy": "Medium",
    # Low
    "gym": "Low",
}

_CATEGORY_WHY: Dict[str, str] = {
    "hospital": "Hospitals drive recurring staff BLS / first-responder certification.",
    "urgent_care": "Urgent-care staff need ongoing BLS certification.",
    "ems": "EMS crews require recurring BLS / ACLS certification.",
    "fire_station": "Fire crews need recurring BLS and first-aid certification.",
    "nursing_school": "Nursing students need CPR / BLS before clinical rotations.",
    "medical_school": "Medical students need BLS / ACLS certification.",
    "cna_training": "CNA programs require CPR certification to graduate.",
    "emt_training": "EMT programs require BLS certification.",
    "healthcare_training": "Allied-health programs mandate CPR / BLS certification.",
    "community_college": "Allied-health and PE students need CPR certification.",
    "university": "Large student populations create steady CPR / first-aid demand.",
    "medical_clinic": "Clinic staff need ongoing BLS certification.",
    "dental_school": "Dental students need BLS certification.",
    "dental_clinic": "Dental offices must keep staff BLS-certified.",
    "senior_care": "Senior-care staff need recurring CPR certification.",
    "childcare_center": "State law mandates CPR / first-aid for childcare staff.",
    "physical_therapy": "PT clinics need staff CPR certification.",
    "gym": "Fitness staff often need basic CPR / first-aid.",
}


def category_importance(key: str) -> str:
    return _CATEGORY_IMPORTANCE.get(key, "Low")


def category_why(key: str) -> str:
    return _CATEGORY_WHY.get(key, "Contributes to local CPR / first-aid demand.")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _bucket_count(competition_summary: Dict[str, Any], mi: int = 5) -> int:
    """Read a distance-bucket count tolerating int OR str keys (JSON-safe)."""
    buckets = competition_summary.get("competitor_count_by_bucket_mi") or {}
    val = buckets.get(mi)
    if val is None:
        val = buckets.get(str(mi))
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def confidence_label(score: Any) -> str:
    s = _num(score)
    if s is None:
        return "unknown"
    if s >= 80:
        return "Very high"
    if s >= 60:
        return "High"
    if s >= 40:
        return "Moderate"
    if s >= 20:
        return "Low"
    return "Very low"


def score_bar(value: Any, width: int = 10) -> str:
    """Return a text meter like '████████░░' for a 0..100 value."""
    s = _num(value)
    if s is None:
        return "░" * width + " unknown"
    s = max(0.0, min(100.0, s))
    filled = int(round(s / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


# --------------------------------------------------------------------------- #
# Expansion readiness (point 4)
# --------------------------------------------------------------------------- #

_READINESS_ORDER = ["Weak", "Moderate", "Strong"]


def _cap_readiness(current: str, ceiling: str) -> str:
    return _READINESS_ORDER[
        min(_READINESS_ORDER.index(current), _READINESS_ORDER.index(ceiling))
    ]


def expansion_readiness(scored: Dict[str, Any]) -> Dict[str, Any]:
    """Compute expansion readiness (Strong / Moderate / Weak) + reasons.

    Rules:
      - Strong: site_score >= 80 and confidence >= 60 and rent known
      - Moderate: site_score >= 65 and confidence >= 35
      - Weak: below that
      - Saturated market with a low competition gap caps at Moderate
      - Unknown rent caps at Moderate
      - Confidence < 30 caps at Weak
    """
    site_score = _num(scored.get("area_score"))
    if site_score is None:
        site_score = _num(scored.get("site_score")) or 0.0
    sub = scored.get("sub_scores") or {}
    confidence = _num(sub.get("confidence_score")) or 0.0
    rent = scored.get("rent") or {}
    rent_conf = str(rent.get("rent_data_confidence") or "unknown")
    rent_known = rent_conf not in ("unknown", "", "none")
    effective_saturation = _num(
        (scored.get("competition_breakdown") or {}).get("effective_saturation")
    ) or 0.0
    competition_gap = _num(sub.get("competition_gap_score")) or 0.0

    reasons: List[str] = []
    if site_score >= 80 and confidence >= 60 and rent_known:
        readiness = "Strong"
        reasons.append(
            f"Site score {site_score:.0f} and confidence {confidence:.0f} are both high."
        )
    elif site_score >= 65 and confidence >= 35:
        readiness = "Moderate"
        reasons.append(
            f"Site score {site_score:.0f} is solid but confidence {confidence:.0f} "
            f"keeps this short of 'Strong'."
        )
    else:
        readiness = "Weak"
        reasons.append(
            f"Site score {site_score:.0f} / confidence {confidence:.0f} are below "
            f"the bar for a confident expansion call."
        )

    if effective_saturation >= 0.8 and competition_gap < 35:
        capped = _cap_readiness(readiness, "Moderate")
        if capped != readiness:
            reasons.append(
                "Capped at Moderate: the CPR/BLS market is saturated and the "
                "competition gap is small."
            )
        readiness = capped

    if not rent_known:
        capped = _cap_readiness(readiness, "Moderate")
        if capped != readiness:
            reasons.append(
                "Capped at Moderate: commercial rent is unknown, so profitability "
                "cannot be trusted yet."
            )
        readiness = capped

    if confidence < 30:
        capped = _cap_readiness(readiness, "Weak")
        if capped != readiness:
            reasons.append(
                f"Capped at Weak: data confidence ({confidence:.0f}) is too low to "
                f"act on."
            )
        readiness = capped

    return {"readiness": readiness, "reasons": reasons}


# --------------------------------------------------------------------------- #
# Demand signals ranked by business importance (point 5)
# --------------------------------------------------------------------------- #

def demand_signals_ranked(profile: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Split nearby demand drivers into high-value vs. secondary signals."""
    counts: Dict[str, Any] = profile.get("counts_5mi") or {}
    saturated: set = set(profile.get("saturated_demand_categories") or [])
    rows: List[Dict[str, Any]] = []
    for key, raw in counts.items():
        try:
            count = int(raw or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        importance = category_importance(key)
        is_saturated = key in saturated
        rows.append({
            "signal": key.replace("_", " "),
            "key": key,
            "count": count,
            "count_display": f"≥{count}" if is_saturated else str(count),
            "saturated": is_saturated,
            "importance": importance,
            "importance_rank": IMPORTANCE_TIERS.get(importance, 3),
            "why": category_why(key),
        })
    rows.sort(key=lambda r: (r["importance_rank"], -r["count"], r["signal"]))
    high_value = [r for r in rows if r["importance_rank"] <= 1]
    secondary = [r for r in rows if r["importance_rank"] >= 2]
    return {"high_value": high_value, "secondary": secondary}


# --------------------------------------------------------------------------- #
# Strategy recommendation labels (point 6)
# --------------------------------------------------------------------------- #

# Short phrasing used when summarising strategies into one sentence.
_STRATEGY_PHRASE: Dict[str, str] = {
    "Nursing Student Certification Hub": "nursing-student certification",
    "Hospital-Adjacent BLS Renewal Center": "hospital BLS renewals",
    "EMS / Fire Workforce Training Node": "EMS/fire workforce training",
    "Airport / Corporate Workforce CPR Center": "airport/corporate workforce CPR",
    "Childcare CPR Certification Center": "childcare CPR certification",
    "Weekend Fast-Certification Center": "weekend fast-certification classes",
    "Multilingual Community CPR Center": "multilingual community classes",
    "Partnership-First Market Entry": "school/employer partnerships",
}

# Stable short keys for each strategy label — used by the --fit-strategy
# report filter so the strategy can be named on the command line.
STRATEGY_KEYS: Dict[str, str] = {
    "Nursing Student Certification Hub": "nursing",
    "Hospital-Adjacent BLS Renewal Center": "hospital",
    "EMS / Fire Workforce Training Node": "ems-fire",
    "Airport / Corporate Workforce CPR Center": "airport",
    "Childcare CPR Certification Center": "childcare",
    "Weekend Fast-Certification Center": "weekend",
    "Multilingual Community CPR Center": "multilingual",
    "Partnership-First Market Entry": "partnership",
}


def _airport_corridor_within(profile: Dict[str, Any], miles: float) -> bool:
    signals = ((profile.get("accessibility") or {}).get("signals") or {})
    sig = signals.get("airport_business_corridor_proximity") or {}
    if not isinstance(sig, dict):
        return False
    if str(sig.get("status")) != "detected":
        return False
    dist = _num(sig.get("distance_miles"))
    return dist is not None and dist <= miles


def strategy_recommendations(
    profile: Dict[str, Any], scored: Dict[str, Any]
) -> List[Dict[str, str]]:
    """Pick the top 1-3 go-to-market strategies from real nearby signals."""
    counts: Dict[str, Any] = profile.get("counts_5mi") or {}

    def c(key: str) -> int:
        try:
            return int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    candidates: List[tuple] = []  # (weight, label, why)

    nursing_like = (
        c("nursing_school") + c("cna_training") + c("emt_training")
        + c("healthcare_training") + c("medical_school")
    )
    if c("nursing_school") >= 1 or nursing_like >= 2:
        candidates.append((
            5 + nursing_like * 3,
            "Nursing Student Certification Hub",
            f"{nursing_like} nursing / healthcare training program(s) nearby need "
            f"pre-clinical CPR/BLS certification.",
        ))

    if c("hospital") >= 1:
        candidates.append((
            4 + c("hospital") * 2,
            "Hospital-Adjacent BLS Renewal Center",
            f"{c('hospital')} hospital(s) within 5 mi drive recurring staff BLS "
            f"renewals.",
        ))

    responders = c("fire_station") + c("ems")
    if responders >= 1:
        candidates.append((
            3 + responders * 2,
            "EMS / Fire Workforce Training Node",
            f"{responders} fire/EMS station(s) nearby need recurring BLS "
            f"certification.",
        ))

    if _airport_corridor_within(profile, 1.0):
        candidates.append((
            6,
            "Airport / Corporate Workforce CPR Center",
            "Airport / business corridor detected within 1 mile — corporate "
            "workforce CPR demand.",
        ))

    if c("childcare_center") >= 3:
        candidates.append((
            2 + c("childcare_center"),
            "Childcare CPR Certification Center",
            f"{c('childcare_center')} childcare centers nearby need "
            f"state-mandated CPR/first-aid certification.",
        ))

    students = c("university") + c("community_college")
    if students >= 2:
        candidates.append((
            2 + students,
            "Weekend Fast-Certification Center",
            f"{students} college(s)/universit(ies) nearby — weekend/evening "
            f"fast-cert classes fit student schedules.",
        ))

    economy = ((profile.get("economy") or {}).get("census") or {})
    population = _num((economy.get("values") or {}).get("population"))
    bachelors = _num((economy.get("indicators") or {}).get("bachelors_or_higher_share"))
    if population is not None and population >= 100_000 and \
            bachelors is not None and bachelors < 0.30:
        candidates.append((
            4,
            "Multilingual Community CPR Center",
            f"Large population ({population:,.0f}) with a lower college-degree "
            f"share — multilingual community classes can capture under-served "
            f"demand.",
        ))

    saturation = _num(
        (scored.get("competition_breakdown") or {}).get("effective_saturation")
    ) or 0.0
    comp_5mi = _bucket_count(profile.get("competition_summary") or {}, 5)
    if saturation >= 0.7 or comp_5mi >= 6:
        candidates.append((
            5 + min(comp_5mi, 6),
            "Partnership-First Market Entry",
            f"{comp_5mi} CPR/BLS competitor(s) nearby — direct consumer search "
            f"will be expensive, so lead with school/employer partnerships.",
        ))

    if not candidates:
        candidates.append((
            1,
            "Partnership-First Market Entry",
            "Limited differentiating signals nearby — enter via targeted "
            "school and employer partnerships.",
        ))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [
        {
            "label": label,
            "why": why,
            "key": STRATEGY_KEYS.get(label, "partnership"),
        }
        for _, label, why in candidates[:3]
    ]


def candidate_strategy_keys(
    profile: Dict[str, Any], scored: Dict[str, Any]
) -> set:
    """Return the set of strategy keys recommended for one candidate."""
    return {s["key"] for s in strategy_recommendations(profile, scored)}


def candidate_matches_strategies(
    profile: Dict[str, Any], scored: Dict[str, Any], wanted_keys: Any
) -> bool:
    """True when a candidate's recommended strategies overlap `wanted_keys`.

    An empty `wanted_keys` means "no filter" — every candidate matches.
    """
    wanted = set(wanted_keys or ())
    if not wanted:
        return True
    return bool(candidate_strategy_keys(profile, scored) & wanted)


# --------------------------------------------------------------------------- #
# Competitor market interpretation (point 7)
# --------------------------------------------------------------------------- #

def competitor_interpretation(
    profile: Dict[str, Any], scored: Dict[str, Any]
) -> Dict[str, Any]:
    """Summarise the competitor landscape into density / quality / gap / path."""
    summary = profile.get("competition_summary") or {}
    count_5mi = _bucket_count(summary, 5)
    avg_rating = _num(summary.get("competitor_avg_rating"))

    if count_5mi <= 1:
        density = "Low"
    elif count_5mi <= 4:
        density = "Medium"
    elif count_5mi <= 9:
        density = "High"
    else:
        density = "Very High"

    if avg_rating is None:
        quality = "Unknown"
    elif avg_rating >= 4.5:
        quality = "Strong"
    elif avg_rating >= 3.8:
        quality = "Mixed"
    else:
        quality = "Weak"

    gap_score = _num((scored.get("sub_scores") or {}).get("competition_gap_score")) or 0.0
    if gap_score >= 65:
        market_gap = "High"
    elif gap_score >= 35:
        market_gap = "Medium"
    else:
        market_gap = "Low"

    if density in ("High", "Very High"):
        if quality == "Strong":
            win_path = (
                "The market is crowded with well-rated competitors. ALLCPR should "
                "not rely on generic 'CPR classes near me' search — win via "
                "partnerships with schools, employers and workforce groups."
            )
        else:
            win_path = (
                "Competitors are numerous but uneven in quality. ALLCPR can win "
                "with a stronger digital-first booking experience plus targeted "
                "school and employer partnerships."
            )
    elif density == "Medium":
        win_path = (
            "Competition is moderate. ALLCPR can compete on consumer search but "
            "should still secure a few anchor partnerships to stabilise demand."
        )
    else:
        win_path = (
            "Competition is light. ALLCPR can lead with direct consumer search "
            "and fast-certification convenience before partnerships."
        )

    return {
        "density": density,
        "quality": quality,
        "market_gap": market_gap,
        "competitor_count_5mi": count_5mi,
        "avg_rating": avg_rating,
        "win_path": win_path,
    }


# --------------------------------------------------------------------------- #
# Plain-English warnings (point 11)
# --------------------------------------------------------------------------- #

def plain_warnings(scored: Dict[str, Any], profile: Dict[str, Any]) -> List[str]:
    """Translate raw risk strings into plain-English warnings."""
    out: List[str] = []
    seen: set = set()

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            out.append(text)

    viability = profile.get("viability") or {}
    if viability.get("needs_validation"):
        anchor = profile.get("anchor") or {}
        anchor_name = anchor.get("name") or "the nearest landmark"
        add(
            f"No commercial storefront anchor was identified — the nearest "
            f"hit was \"{anchor_name}\". Treat this candidate as a "
            f"coordinate to validate with a commercial broker, not as a "
            f"confirmed business location."
        )

    for risk in scored.get("risks") or []:
        low = str(risk).lower()
        if "rent" in low:
            add("Rent is unknown, so profitability cannot be trusted yet.")
        elif "saturat" in low:
            add(
                "There are already many CPR/BLS providers nearby, so ALLCPR needs "
                "a clear differentiation strategy."
            )
        elif "confidence" in low:
            add(
                "Some underlying data is missing, so treat this recommendation as "
                "preliminary until validated."
            )
        elif "census" in low or "economy" in low:
            add(
                "Local economic data is incomplete, so the demographic fit is "
                "uncertain."
            )
        elif "job-posting" in low or "job posting" in low:
            add(
                "Employer (B2B) certification demand has not been verified yet."
            )
        else:
            add(str(risk))

    missing = profile.get("missing_fields") or []
    if missing and not any("missing data" in w.lower() for w in out):
        add(
            f"{len(missing)} data field(s) could not be collected — validate them "
            f"before committing to a lease."
        )
    return out


# --------------------------------------------------------------------------- #
# Per-candidate quick read (point 2)
# --------------------------------------------------------------------------- #

def _location_descriptor(profile: Dict[str, Any]) -> str:
    counts = profile.get("counts_5mi") or {}

    def c(key: str) -> int:
        try:
            return int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    healthcare_clinical = c("hospital") + c("urgent_care") + c("medical_clinic")
    education = c("nursing_school") + c("university") + c("community_college")
    near_airport = _airport_corridor_within(profile, 1.5)

    # Healthcare- and education-heavy descriptors win over airport corridor:
    # an N First St SJC-adjacent anchor surrounded by hospitals + nursing
    # schools should read as healthcare-driven, not as an airport site.
    if education >= 4 and c("nursing_school") + c("medical_school") >= 2:
        return "Education corridor with a strong student / healthcare-pipeline base."
    if healthcare_clinical >= 5:
        return "Healthcare-adjacent area with steady clinical certification demand."
    if near_airport:
        return "Airport / business corridor near healthcare training demand."
    if healthcare_clinical >= 3:
        return "Healthcare-adjacent area with steady clinical certification demand."
    if education >= 2:
        return "Education corridor with a strong student / healthcare-pipeline base."
    if c("childcare_center") >= 5:
        return "Family / childcare-dense area with mandated CPR demand."
    return "Mixed-use area with moderate CPR / first-aid demand."


def candidate_quick_read(
    profile: Dict[str, Any], scored: Dict[str, Any],
    interp: Dict[str, Any],
) -> Dict[str, str]:
    """Build the plain-English 'Quick read' card for one candidate."""
    sub = scored.get("sub_scores") or {}
    meter_labels = {
        "demand_score": "demand density",
        "healthcare_training_ecosystem_score": "training ecosystem",
        "competition_gap_score": "competition gap",
        "allcpr_opportunity_score": "ALLCPR opportunity",
        "economy_score": "economy",
        "accessibility_score": "accessibility",
    }
    scored_pairs = [
        (label, _num(sub.get(key)))
        for key, label in meter_labels.items()
    ]
    rated = [(lbl, v) for lbl, v in scored_pairs if v is not None]
    strengths = sorted(rated, key=lambda t: t[1], reverse=True)[:3]
    weaknesses = sorted(rated, key=lambda t: t[1])[:2]

    why_high = (
        "Strong " + ", ".join(lbl for lbl, _ in strengths) + "."
        if strengths else "No standout strengths in the collected signals."
    )
    why_fail = (
        "Weakest on " + ", ".join(lbl for lbl, _ in weaknesses) + "."
        if weaknesses else "No obvious weak points, but data is thin."
    )
    warnings = plain_warnings(scored, profile)
    if warnings:
        why_fail = why_fail + " " + warnings[0]

    strategies = interp.get("strategies") or []
    best_use = strategies[0]["label"] if strategies else "Partnership-driven CPR center."

    readiness = (interp.get("expansion_readiness") or {}).get("readiness", "Weak")
    decision = {
        "Strong": "Ready to shortlist for site visits.",
        "Moderate": "Worth validating, not ready for lease yet.",
        "Weak": "Not recommended without significant new data.",
    }[readiness]

    return {
        "what": _location_descriptor(profile),
        "why_high": why_high,
        "why_fail": why_fail,
        "best_use": best_use,
        "decision": decision,
    }


# --------------------------------------------------------------------------- #
# Decision checklist (point 9) + next actions (point 10)
# --------------------------------------------------------------------------- #

def decision_checklist() -> List[str]:
    return [
        "Confirm an available commercial unit within 0.5 mi of the anchor",
        "Confirm parking and public transit access",
        "Estimate monthly rent and add a cited rent override",
        "Call the top 5 nearby schools / healthcare institutions",
        "Check whether nearby competitors have full class schedules",
        "Run a weekend / evening demand test campaign",
    ]


def top_3_actions(profile: Optional[Dict[str, Any]]) -> List[str]:
    anchor = (profile or {}).get("anchor") or {}
    name = anchor.get("name") or (profile or {}).get("candidate_name") or "the top candidate"
    return [
        f"Validate rent and parking near {name}.",
        "Contact nearby healthcare schools and employers to gauge partnership interest.",
        "Run a small paid-search / landing-page test before signing a lease.",
    ]


# --------------------------------------------------------------------------- #
# Score meters (point 3)
# --------------------------------------------------------------------------- #

# (label, sub_score key) — order shown in the report.
METER_FIELDS: List[tuple] = [
    ("Demand", "demand_score"),
    ("Training ecosystem", "healthcare_training_ecosystem_score"),
    ("Competition gap", "competition_gap_score"),
    ("ALLCPR opportunity", "allcpr_opportunity_score"),
    ("Economy", "economy_score"),
    ("Accessibility", "accessibility_score"),
    ("Confidence", "confidence_score"),
]


def score_meters(scored: Dict[str, Any]) -> List[Dict[str, Any]]:
    sub = scored.get("sub_scores") or {}
    out: List[Dict[str, Any]] = []
    for label, key in METER_FIELDS:
        out.append({"label": label, "key": key, "value": _num(sub.get(key))})
    return out


# --------------------------------------------------------------------------- #
# Bundled per-candidate interpretation + report-level executive verdict
# --------------------------------------------------------------------------- #

def build_candidate_interpretation(
    profile: Dict[str, Any], scored: Dict[str, Any],
    cohort_means: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Bundle every per-candidate interpretation artefact into one dict.

    When ``cohort_means`` is supplied (multi-candidate runs), a factor
    decomposition is attached: each sub-score's signed contribution to this
    candidate's Δ-vs-cohort, sorted by magnitude.
    """
    interp: Dict[str, Any] = {
        "expansion_readiness": expansion_readiness(scored),
        "demand_signals": demand_signals_ranked(profile),
        "strategies": strategy_recommendations(profile, scored),
        "competitor_interpretation": competitor_interpretation(profile, scored),
        "warnings": plain_warnings(scored, profile),
        "score_meters": score_meters(scored),
        "decision_checklist": decision_checklist(),
        "opportunity_gaps": compute_opportunity_gaps(
            profile.get("competition_summary") or {}
        ),
    }
    if cohort_means:
        decomposition = factor_decomposition(scored, cohort_means)
        interp["factor_decomposition"] = decomposition
        interp["factor_decomposition_text"] = _decomposition_text(decomposition)
    interp["quick_read"] = candidate_quick_read(profile, scored, interp)
    return interp


def _candidate_title(profile: Dict[str, Any]) -> str:
    anchor = profile.get("anchor") or {}
    viability = profile.get("viability") or {}
    if viability.get("needs_validation"):
        return (
            profile.get("candidate_name")
            or "Needs commercial site validation"
        )
    return (
        anchor.get("name")
        or profile.get("candidate_name")
        or profile.get("comparison_area")
        or profile.get("candidate_id")
        or "unknown candidate"
    )


def executive_verdict(
    profile: Dict[str, Any], scored: Dict[str, Any],
    interp: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the top-of-report executive verdict for the best candidate."""
    tier = str(scored.get("tier") or "F")
    tier_verdict = {
        "A": "Strong location",
        "B": "Promising — validate first",
        "C": "Mixed — needs more data",
        "D": "Not recommended",
        "F": "Avoid",
    }.get(tier, "Needs review")

    comp = interp.get("competitor_interpretation") or {}
    if comp.get("density") in ("High", "Very High"):
        verdict = f"{tier_verdict}, but competition-heavy"
    else:
        verdict = tier_verdict

    demand = interp.get("demand_signals") or {}
    high_value = demand.get("high_value") or []
    if high_value:
        top_signals = ", ".join(r["signal"] for r in high_value[:2])
        why = f"Strong {top_signals} demand near the anchor."
    else:
        why = "Demand drivers nearby are limited — upside is uncertain."

    warnings = interp.get("warnings") or []
    biggest_risk = warnings[0] if warnings else (
        "No major risk flagged, but on-site validation is still required."
    )

    strategies = interp.get("strategies") or []
    if strategies:
        phrases = [
            _STRATEGY_PHRASE.get(s["label"], s["label"].lower())
            for s in strategies
        ]
        best_strategy = "Target " + ", ".join(phrases) + "."
    else:
        best_strategy = "Lead with school and employer partnerships."

    sub = scored.get("sub_scores") or {}
    conf = _num(sub.get("confidence_score"))
    confidence = (
        f"{confidence_label(conf)} ({conf:.0f}/100)" if conf is not None
        else "unknown"
    )

    before_leasing_bits: List[str] = []
    rent = scored.get("rent") or {}
    if str(rent.get("rent_data_confidence") or "unknown") in ("unknown", "", "none"):
        before_leasing_bits.append("commercial rent")
    before_leasing_bits.append("parking and site access")
    before_leasing_bits.append("real class demand (calls / site visits)")
    if (profile.get("missing_fields") or []):
        before_leasing_bits.append("the missing data fields listed per candidate")
    before_leasing = "Validate " + ", ".join(before_leasing_bits) + "."

    # Honest framing: a great AREA is not a validated SITE.
    is_site = bool(scored.get("is_site_candidate"))
    best_label = "Best validated site candidate" if is_site else "Best area-level candidate"
    executive_state = str(scored.get("executive_state") or "Recommended for field validation")
    area_score = _num(scored.get("area_score"))
    if area_score is None:
        area_score = _num(scored.get("site_score"))  # legacy fallback
    site_score = scored.get("site_score")
    area_txt = f"{area_score:.1f}/100" if area_score is not None else "unknown"
    if is_site and isinstance(site_score, (int, float)):
        score_line = f"Area {area_txt} · Site {site_score:.1f}/100 (validated)"
    else:
        score_line = (
            f"Area {area_txt} · Site: Not validated — promising area, "
            f"not a confirmed leasing opportunity"
        )

    return {
        "best_candidate": _candidate_title(profile),
        "best_candidate_label": best_label,
        "executive_state": executive_state,
        "score_line": score_line,
        "verdict": verdict,
        "expansion_readiness": (interp.get("expansion_readiness") or {}).get(
            "readiness", "Weak"
        ),
        "why_it_matters": why,
        "biggest_risk": biggest_risk,
        "best_strategy": best_strategy,
        "confidence": confidence,
        "before_leasing": before_leasing,
    }


_ADVANTAGE_LABELS: Dict[str, str] = {
    "demand_score": "demand density",
    "healthcare_training_ecosystem_score": "training ecosystem",
    "competition_gap_score": "competition gap",
    "allcpr_opportunity_score": "ALLCPR opportunity",
    "economy_score": "economy",
    "accessibility_score": "accessibility",
}


def _strongest_advantage(scored: Dict[str, Any]) -> str:
    sub = scored.get("sub_scores") or {}
    rated = [
        (label, _num(sub.get(key)))
        for key, label in _ADVANTAGE_LABELS.items()
    ]
    rated = [(lbl, v) for lbl, v in rated if v is not None]
    if not rated:
        return "no standout strengths"
    rated.sort(key=lambda t: t[1], reverse=True)
    top_label, top_value = rated[0]
    return f"{top_label} ({top_value:.0f}/100)"


def _biggest_operational_risk(
    scored: Dict[str, Any], interp: Dict[str, Any]
) -> str:
    warnings = interp.get("warnings") or []
    if warnings:
        return str(warnings[0])
    sub = scored.get("sub_scores") or {}
    rated = [
        (label, _num(sub.get(key)))
        for key, label in _ADVANTAGE_LABELS.items()
    ]
    rated = [(lbl, v) for lbl, v in rated if v is not None]
    if rated:
        rated.sort(key=lambda t: t[1])
        weakest_label, weakest_value = rated[0]
        return f"weakest on {weakest_label} ({weakest_value:.0f}/100)"
    return "limited data to flag risks"


def _fastest_path_to_profitability(
    profile: Dict[str, Any], scored: Dict[str, Any], interp: Dict[str, Any]
) -> str:
    job = scored.get("job_demand") or {}
    job_score = _num(job.get("job_certification_demand_score"))
    if isinstance(job_score, (int, float)) and job_score >= 50:
        return "Confirmed employer cert-demand — open with B2B contracts."
    strategies = interp.get("strategies") or []
    if strategies:
        top = strategies[0]
        return f"Lead with {_STRATEGY_PHRASE.get(top['label'], top['label'].lower())}."
    return "Lead with school / employer partnerships."


def _launch_difficulty(
    profile: Dict[str, Any], scored: Dict[str, Any], interp: Dict[str, Any]
) -> str:
    comp = interp.get("competitor_interpretation") or {}
    density = str(comp.get("density") or "")
    rent_known = str((scored.get("rent") or {}).get("rent_data_confidence") or "unknown") \
        not in ("unknown", "", "none")
    confidence = _num((scored.get("sub_scores") or {}).get("confidence_score")) or 0

    hard_signals = 0
    if density in ("High", "Very High"):
        hard_signals += 1
    if not rent_known:
        hard_signals += 1
    if confidence < 40:
        hard_signals += 1

    if hard_signals >= 2:
        return "Hard"
    if hard_signals == 1:
        return "Medium"
    return "Easy"


def decision_matrix(
    ranked: List[Any]
) -> List[Dict[str, Any]]:
    """One-row-per-candidate executive comparison matrix.

    ``ranked`` is a list of ``(profile, scored)`` tuples; the matrix lets an
    executive eyeball strongest advantage / biggest risk / fastest path /
    best strategic fit / launch difficulty for each candidate side-by-side.
    """
    cohort_means = cohort_means_from_ranked(ranked) if len(ranked) > 1 else {}
    rows: List[Dict[str, Any]] = []
    for entry in ranked:
        if not entry:
            continue
        profile, scored = entry[0], entry[1]
        interp = build_candidate_interpretation(
            profile, scored, cohort_means=cohort_means,
        )
        strategies = interp.get("strategies") or []
        best_fit = strategies[0]["label"] if strategies else "Partnership-First Market Entry"
        rows.append({
            "candidate": _candidate_title(profile),
            "site_score": scored.get("site_score"),
            "tier": scored.get("tier"),
            "readiness": (interp.get("expansion_readiness") or {})
                .get("readiness", "Weak"),
            "strongest_advantage": _strongest_advantage(scored),
            "biggest_risk": _biggest_operational_risk(scored, interp),
            "fastest_path_to_profitability":
                _fastest_path_to_profitability(profile, scored, interp),
            "best_strategic_fit": best_fit,
            "launch_difficulty": _launch_difficulty(profile, scored, interp),
        })
    return rows


_FACTOR_LABELS: Dict[str, str] = {
    "demand_score": "demand density",
    "healthcare_training_ecosystem_score": "training ecosystem",
    "competition_gap_score": "competition gap",
    "allcpr_opportunity_score": "ALLCPR opportunity",
    "economy_score": "economy",
    "accessibility_score": "accessibility",
    "historical_performance_score": "historical ALLCPR performance",
    "profitability_score": "profitability",
}


def _decomposition_text(rows: List[Dict[str, Any]], top_n: int = 3) -> str:
    """One-line plain-English summary of the dominant drivers of the Δ."""
    if not rows:
        return "No cohort to compare against (single candidate)."
    drivers = [r for r in rows if abs(r["contribution_to_site_delta"]) >= 0.05]
    if not drivers:
        return "Indistinguishable from cohort mean across all sub-scores."
    parts: List[str] = []
    for r in drivers[:top_n]:
        sign = "+" if r["contribution_to_site_delta"] >= 0 else "−"
        label = _FACTOR_LABELS.get(r["sub_score"], r["sub_score"])
        parts.append(
            f"{sign}{abs(r['contribution_to_site_delta']):.1f} {label} "
            f"({r['value']:.0f} vs cohort {r['cohort_mean']:.0f})"
        )
    return "Drivers: " + "; ".join(parts) + "."


# --------------------------------------------------------------------------- #
# Phase 4B — course performance section assembly
# --------------------------------------------------------------------------- #

def aggregate_demand_counts(ranked: List[Any]) -> Dict[str, int]:
    """Sum each candidate's ``counts_5mi`` into one area-level demand map.

    Used to drive the public-demand-vs-actual-enrollment comparison.
    """
    totals: Dict[str, int] = {}
    seen_keys: Dict[str, int] = {}
    for entry in ranked:
        if not entry:
            continue
        profile = entry[0] or {}
        counts = profile.get("counts_5mi") or {}
        for key, raw in counts.items():
            try:
                val = int(raw or 0)
            except (TypeError, ValueError):
                continue
            # Use the max across candidates (they overlap heavily) rather than
            # summing the same hospitals five times.
            seen_keys[key] = max(seen_keys.get(key, 0), val)
    totals.update(seen_keys)
    return totals


def build_course_performance_section(
    records: List[Any],
    city: Optional[str] = None,
    state: Optional[str] = None,
    demand_counts: Optional[Dict[str, int]] = None,
    demand: Optional[Dict[str, Any]] = None,
    competition: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Assemble the scored, report-ready course-performance payload.

    Returns ``None`` when there are no Enrollware records (caller omits the
    section). The returned dict is JSON-serializable and is what the HTML
    renderer's ``_course_performance_section`` consumes.
    """
    if not records:
        return None

    # Data hygiene: every historical aggregate (scoring, benchmark table,
    # trends, ALLCPR reference) runs on *held* classes only — classes that
    # actually ran with real attendance in a completed month. Enrollware
    # exports carry no cancelled flag, so zero/blank enrollment and
    # future/current-partial months would otherwise pollute every average.
    from app.collectors.enrollware import held_classes
    records = held_classes(records)
    if not records:
        return None

    # Revenue has no price column in Enrollware exports — fall back to ALLCPR's
    # real median course price for a clearly-labeled modeled revenue figure.
    from app.collectors.allcpr_prices import lookup_price
    modeled_price = lookup_price(state).avg_price if state else lookup_price(None).avg_price

    # Company-wide reference average (unfiltered) so each area's course types
    # can be compared against overall ALLCPR behavior, not just the local mean.
    allcpr_overall = build_course_performance(records, city=None, state=None)
    allcpr_overall_avg = (
        (allcpr_overall or {}).get("overall", {}).get("average_students_per_class")
    )
    from app.evaluation.course_enrollment_benchmarks import (
        build_course_enrollment_benchmarks,
    )
    from app.evaluation.course_enrollment_trends import (
        build_course_enrollment_trends,
    )
    course_benchmarks = build_course_enrollment_benchmarks(records)
    course_trends = build_course_enrollment_trends(records)

    perf = build_course_performance(
        records, city=city, state=state, modeled_price=modeled_price,
    )
    if not perf:
        return None
    perf["course_enrollment_benchmarks"] = course_benchmarks
    perf["course_enrollment_trends"] = course_trends
    score_course_performance(perf, allcpr_overall_avg=allcpr_overall_avg)
    if demand_counts:
        perf["public_demand_vs_actual"] = compare_public_demand(perf, demand_counts)

    # Phase 4C additions — same Enrollware records, new lenses. Each builder is
    # fail-soft (returns None when the data can't support it) so the section
    # simply omits whatever can't be computed; nothing is invented.
    from app.enrichers.course_performance import (
        _filter_records,
        rollup_decision_records,
    )
    from app.enrichers.location_performance import build_location_performance
    from app.enrichers.schedule_intelligence import build_schedule_intelligence
    from app.scoring.forecasting import build_forecast

    area_records = _filter_records(records, city)
    schedule = build_schedule_intelligence(area_records)
    if schedule:
        perf["schedule_intelligence"] = schedule
    decision_area_records, _ = rollup_decision_records(area_records)
    forecast = build_forecast(decision_area_records, modeled_price=modeled_price)
    if forecast:
        perf["forecast"] = forecast
    # Location grouping stays ALLCPR-wide (every city), not area-filtered.
    locations = build_location_performance(
        records, group_by="city", modeled_price=modeled_price,
    )
    if locations:
        perf["location_performance"] = locations

    # Phase 5 — honest, deterministic course opportunity graph. Built from the
    # course data already assembled above plus optional area-level public-demand
    # and competition signals. Powers Primary/Secondary/Avoid when present; the
    # report falls back to the strategy block above when it is absent.
    from app.evaluation.evaluation_pipeline import build_evaluation_graph
    evaluation_graph = build_evaluation_graph(
        perf,
        demand=demand,
        competition=competition,
        allcpr_overall_avg=allcpr_overall_avg,
    )
    if evaluation_graph:
        perf["evaluation_graph"] = evaluation_graph

    # Score vs Actual Enrollment Validation — honest sanity-check of whether the
    # generated opportunity score has historically tracked actual enrollment.
    from app.scoring.regression_validation import build_regression_validation
    perf["regression_validation"] = build_regression_validation(perf)

    # Center-opening decision per course (Open / Test / Watch / Avoid) — a thin
    # mapping over the evaluation graph, no new scoring.
    from app.evaluation.center_opening import build_center_opening_recommendations
    perf["center_opening"] = build_center_opening_recommendations(perf)
    return perf


def build_report_interpretation(ranked: List[Any]) -> Dict[str, Any]:
    """Build report-level interpretation (executive verdict + next actions).

    `ranked` is a list of `(profile, scored)` tuples, best first.
    """
    if not ranked:
        return {
            "executive_verdict": None,
            "next_actions": [
                "Collect more candidate areas — no candidates were evaluated.",
            ],
        }
    cohort_means = cohort_means_from_ranked(ranked)
    top_profile, top_scored = ranked[0]
    interp = build_candidate_interpretation(
        top_profile, top_scored, cohort_means=cohort_means,
    )
    return {
        "executive_verdict": executive_verdict(top_profile, top_scored, interp),
        "next_actions": top_3_actions(top_profile),
        "decision_matrix": decision_matrix(ranked),
        "cohort_means": cohort_means,
    }
