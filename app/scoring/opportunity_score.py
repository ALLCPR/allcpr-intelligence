"""
ALLCPR opportunity score — 0..100.

Answers: "Can ALLCPR realistically win here?" Combines:
  - demand_score:                 strong demand nearby raises ceiling
  - training_ecosystem_score:     student/healthcare-pro pipeline
  - competition_gap_score:        saturation gap (already accounts for weak comps)
  - competitor weakness signals:  share of competitors without website / phone,
                                  share with rating < 4.0 — each opens room
                                  for a stronger digital-first operator

The signal is intentionally conservative: missing data does not boost the score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class OpportunityBreakdown:
    score: float
    rationale: List[str]
    angles: List[str]   # suggested go-to-market angles
    weakness_index: float   # 0..1, share of competitors with weak digital presence
    white_space_score: Optional[float] = None  # demand × growth / competition


def _weakness_index(competition_summary: Dict[str, object]) -> float:
    total = int(competition_summary.get("competitor_count_total") or 0)
    if total <= 0:
        return 0.0
    no_site = int(competition_summary.get("competitor_no_website") or 0)
    no_phone = int(competition_summary.get("competitor_no_phone") or 0)
    low_rated = int(competition_summary.get("competitor_low_rating_count") or 0)
    booking_missing = int(competition_summary.get("competitor_online_booking_missing") or 0)
    schedule_missing = int(competition_summary.get("competitor_class_schedule_missing") or 0)
    pricing_missing = int(competition_summary.get("competitor_pricing_missing") or 0)
    contact_friction = int(
        competition_summary.get("competitor_contact_friction_detected") or 0
    )
    outdated = int(competition_summary.get("competitor_outdated_website_detected") or 0)
    # Each axis weighted equally; cap at 1.0.
    num = (
        no_site
        + no_phone
        + 2 * low_rated
        + booking_missing
        + schedule_missing
        + pricing_missing
        + contact_friction
        + outdated
    )
    denom_axes = 9

    # Differentiation gaps — competitors lacking weekend classes, fast-cert
    # options, or multilingual positioning are openings for ALLCPR. These keys
    # are only present once competitor website_analysis populates them; until
    # then they're absent and DO NOT change the score (denominator stays 9).
    for key in (
        "competitor_weekend_classes_missing",
        "competitor_fast_cert_missing",
        "competitor_multilingual_missing",
    ):
        if key in competition_summary:
            num += int(competition_summary.get(key) or 0)
            denom_axes += 1

    raw = num / (denom_axes * total)
    return min(1.0, max(0.0, raw))


def _angles_for(demand_breakdown: Dict[str, float],
                training_breakdown: Dict[str, float]) -> List[str]:
    """Pick a few plausible go-to-market angles from what's actually nearby."""
    out: List[str] = []
    if (training_breakdown.get("nursing_school", 0) or 0) >= 0.5:
        out.append("nursing-student certification hub")
    if (demand_breakdown.get("hospital", 0) or 0) >= 0.5:
        out.append("hospital-adjacent BLS renewal center")
    if (demand_breakdown.get("childcare_center", 0) or 0) >= 0.4:
        out.append("childcare CPR certification (state-mandated)")
    if (demand_breakdown.get("senior_care", 0) or 0) >= 0.3:
        out.append("senior-care staff recertification")
    if (training_breakdown.get("community_college", 0) or 0) >= 0.4:
        out.append("community-college partnership program")
    if not out:
        out.append("employer group training (B2B outreach)")
    out.append("weekend / evening fast-cert classes")
    return out[:5]


def _compute_white_space(
    demand_norm: float,
    growth_proxy: Optional[float],
    pressure_score_0_100: Optional[float],
    weakness: float,
) -> Optional[float]:
    """White-space opportunity: (demand × growth) / effective competition.

    Returns ``None`` when none of the inputs are usable. Effective competition
    strength is reduced by ``weakness`` so a crowded market full of weak
    operators reads as more open than a crowded market of strong incumbents.
    """
    if demand_norm <= 0:
        return None
    growth = growth_proxy if growth_proxy is not None else 0.6
    growth = max(0.2, min(1.5, growth))
    if pressure_score_0_100 is None:
        # No competitors detected — white space is effectively the demand
        # itself, lightly boosted by growth.
        return round(min(1.0, demand_norm * growth) * 100.0, 2)
    pressure_n = max(0.0, min(1.0, pressure_score_0_100 / 100.0))
    effective_competition = max(0.1, pressure_n * (1.0 - 0.5 * weakness))
    raw = (demand_norm * growth) / effective_competition
    return round(min(1.0, raw / 2.0) * 100.0, 2)


def compute_opportunity_score(
    demand_score_0_100: float,
    training_score_0_100: float,
    competition_gap_score_0_100: float,
    competition_summary: Dict[str, object],
    demand_breakdown: Optional[Dict[str, float]] = None,
    training_breakdown: Optional[Dict[str, float]] = None,
    job_demand_score_0_100: Optional[float] = None,
    competition_pressure_score_0_100: Optional[float] = None,
    growth_proxy: Optional[float] = None,
) -> OpportunityBreakdown:
    demand_n = max(0.0, min(1.0, demand_score_0_100 / 100.0))
    train_n = max(0.0, min(1.0, training_score_0_100 / 100.0))
    gap_n = max(0.0, min(1.0, competition_gap_score_0_100 / 100.0))
    weakness = _weakness_index(competition_summary)
    job_n = (
        max(0.0, min(1.0, job_demand_score_0_100 / 100.0))
        if isinstance(job_demand_score_0_100, (int, float))
        else None
    )
    white_space_score = _compute_white_space(
        demand_norm=demand_n,
        growth_proxy=growth_proxy,
        pressure_score_0_100=competition_pressure_score_0_100,
        weakness=weakness,
    )
    white_space_n = (
        max(0.0, min(1.0, white_space_score / 100.0))
        if white_space_score is not None else None
    )

    # Weighted blend. White-space (demand × growth / competition) replaces some
    # of the linear gap term so high-demand crowded markets — where overall
    # certification volume is enormous — don't get blindly penalized.
    if white_space_n is None:
        if job_n is None:
            base = (0.35 * demand_n
                    + 0.25 * train_n
                    + 0.25 * gap_n
                    + 0.15 * weakness)
        else:
            base = (0.30 * demand_n
                    + 0.20 * train_n
                    + 0.25 * gap_n
                    + 0.15 * weakness
                    + 0.10 * job_n)
    else:
        if job_n is None:
            base = (0.30 * demand_n
                    + 0.20 * train_n
                    + 0.15 * gap_n
                    + 0.15 * weakness
                    + 0.20 * white_space_n)
        else:
            base = (0.25 * demand_n
                    + 0.15 * train_n
                    + 0.15 * gap_n
                    + 0.15 * weakness
                    + 0.10 * job_n
                    + 0.20 * white_space_n)
    score = round(base * 100.0, 2)

    bullets: List[str] = []
    if demand_n >= 0.6:
        bullets.append("strong nearby demand")
    elif demand_n < 0.3:
        bullets.append("thin nearby demand")
    if train_n >= 0.6:
        bullets.append("healthcare-training ecosystem present")
    if gap_n >= 0.5:
        bullets.append("competition gap is real")
    elif gap_n < 0.25:
        bullets.append("market is saturated — entry will be harder")
    if weakness >= 0.4:
        bullets.append(
            f"weak competitor digital presence "
            f"({weakness:.0%} no-website/phone/low-rating share)"
        )
    elif weakness <= 0.1:
        bullets.append("competitors are well-established online")
    if job_n is not None and job_n >= 0.4:
        bullets.append("public job postings show certification demand")
    elif job_n is not None and job_n <= 0.1:
        bullets.append("supplied job postings show little certification demand")
    if white_space_score is not None:
        if white_space_score >= 60:
            bullets.append(
                f"white-space opportunity high ({white_space_score:.0f}/100) — "
                f"demand outpaces effective competition"
            )
        elif white_space_score <= 25:
            bullets.append(
                f"white-space opportunity low ({white_space_score:.0f}/100) — "
                f"strong incumbents absorb existing demand"
            )

    angles = _angles_for(demand_breakdown or {}, training_breakdown or {})
    if job_n is not None and job_n >= 0.4:
        angles.insert(0, "employer certification-demand outreach")
        angles = angles[:5]

    return OpportunityBreakdown(
        score=score,
        rationale=bullets,
        angles=angles,
        weakness_index=round(weakness, 3),
        white_space_score=white_space_score,
    )
