"""
Opportunity gap engine.

Converts the aggregate competitor website-analysis signals into a list of
actionable market gaps and a short positioning hint per candidate. This is
deterministic — no AI, no inference beyond what the aggregates support.

A "gap" is an opportunity to differentiate, evidenced by the share of nearby
competitors that *lack* a capability (online booking, weekend classes, ACLS/
PALS offerings, multilingual support, etc.). Gaps only carry weight when the
underlying website-analysis check actually ran for enough competitors — if
nothing was checked, gaps are reported as ``data_confidence: "low"`` and
treated as unknown.

Each gap dict contains:
- ``key``: stable machine key (e.g. ``"online_booking_gap"``)
- ``label``: short title for the report
- ``strength``: ``"strong"`` / ``"moderate"`` / ``"weak"`` / ``"none"``
- ``evidence``: one-sentence summary citing counts
- ``recommendation``: deterministic positioning hint
- ``data_confidence``: ``"high"`` / ``"medium"`` / ``"low"``
"""
from __future__ import annotations

from typing import Dict, List


def _i(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _confidence_from_checked(checked: int, total: int) -> str:
    if total <= 0 or checked <= 0:
        return "low"
    ratio = checked / total
    if ratio >= 0.6:
        return "high"
    if ratio >= 0.3:
        return "medium"
    return "low"


def _strength_from_share(share: float) -> str:
    if share >= 0.6:
        return "strong"
    if share >= 0.35:
        return "moderate"
    if share > 0:
        return "weak"
    return "none"


def _gap(
    key: str,
    label: str,
    missing: int,
    offered: int,
    total: int,
    checked: int,
    recommendation: str,
) -> Dict[str, object]:
    """Build one gap row. The 'share' is missing/checked when meaningful."""
    denom = max(checked, 1)
    share = missing / denom if checked > 0 else 0.0
    strength = _strength_from_share(share) if checked > 0 else "none"
    confidence = _confidence_from_checked(checked, total)
    if checked == 0:
        evidence = (
            f"Competitor website analysis did not run — {key} gap unknown."
        )
    else:
        evidence = (
            f"{missing} of {checked} analyzed competitors lack this; "
            f"{offered} clearly offer it."
        )
    return {
        "key": key,
        "label": label,
        "strength": strength,
        "share_missing": round(share, 3),
        "missing_count": missing,
        "offered_count": offered,
        "checked_count": checked,
        "evidence": evidence,
        "recommendation": recommendation,
        "data_confidence": confidence,
    }


def compute_opportunity_gaps(competition_summary: Dict[str, object]) -> Dict[str, object]:
    """Return ``{"gaps": [...], "positioning": str, "data_confidence": str}``.

    ``positioning`` is a one-sentence headline summarising the top 1-2 strong
    gaps, or a hedged note when data confidence is low.
    """
    total = _i(competition_summary.get("competitor_count_total"))
    checked = _i(competition_summary.get("website_analysis_checked_count"))

    gaps: List[Dict[str, object]] = []

    gaps.append(_gap(
        "online_booking_gap",
        "Online booking gap",
        missing=_i(competition_summary.get("competitor_online_booking_missing")),
        offered=max(
            0,
            checked
            - _i(competition_summary.get("competitor_online_booking_missing")),
        ),
        total=total,
        checked=checked,
        recommendation=(
            "Lead with a friction-free online booking flow on the homepage; "
            "even one click to register beats most local competitors."
        ),
    ))

    gaps.append(_gap(
        "class_schedule_gap",
        "Class schedule transparency gap",
        missing=_i(competition_summary.get("competitor_class_schedule_missing")),
        offered=max(
            0,
            checked
            - _i(competition_summary.get("competitor_class_schedule_missing")),
        ),
        total=total,
        checked=checked,
        recommendation=(
            "Publish an always-current public schedule with seats remaining; "
            "students will pick the operator who shows availability first."
        ),
    ))

    gaps.append(_gap(
        "pricing_transparency_gap",
        "Pricing transparency gap",
        missing=_i(competition_summary.get("competitor_pricing_missing")),
        offered=max(
            0,
            checked
            - _i(competition_summary.get("competitor_pricing_missing")),
        ),
        total=total,
        checked=checked,
        recommendation=(
            "Show class prices openly; competitors hiding pricing send buyers "
            "to the first operator that lists fees."
        ),
    ))

    gaps.append(_gap(
        "acls_pals_gap",
        "ACLS / PALS offering gap",
        missing=_i(competition_summary.get("competitor_acls_pals_missing")),
        offered=_i(competition_summary.get("competitor_acls_pals_offered")),
        total=total,
        checked=checked,
        recommendation=(
            "Add ACLS / PALS to the catalog — higher per-student price and "
            "fewer local providers than basic CPR."
        ),
    ))

    gaps.append(_gap(
        "weekend_availability_gap",
        "Weekend / evening availability gap",
        missing=_i(competition_summary.get("competitor_weekend_missing")),
        offered=_i(competition_summary.get("competitor_weekend_offered")),
        total=total,
        checked=checked,
        recommendation=(
            "Run weekend and evening fast-cert sessions — students and "
            "shift-workers cannot use weekday-only competitors."
        ),
    ))

    gaps.append(_gap(
        "group_corporate_gap",
        "Group / corporate training gap",
        missing=_i(competition_summary.get("competitor_group_corporate_missing")),
        offered=_i(competition_summary.get("competitor_group_corporate_offered")),
        total=total,
        checked=checked,
        recommendation=(
            "Pitch on-site corporate / group training to local employers; "
            "B2B contracts stabilise demand year-round."
        ),
    ))

    gaps.append(_gap(
        "multilingual_gap",
        "Multilingual offering gap",
        missing=_i(competition_summary.get("competitor_multilingual_missing")),
        offered=_i(competition_summary.get("competitor_multilingual_offered")),
        total=total,
        checked=checked,
        recommendation=(
            "Advertise Spanish (and other top local languages) classes; "
            "few competitors mention multilingual support."
        ),
    ))

    gaps.append(_gap(
        "digital_experience_gap",
        "Digital experience / contact friction gap",
        missing=_i(competition_summary.get("competitor_contact_friction_detected"))
        + _i(competition_summary.get("competitor_outdated_website_detected")),
        offered=0,
        total=total,
        checked=checked,
        recommendation=(
            "Compete on a clean, mobile-first website; many local operators "
            "still ship outdated pages with friction-heavy contact flows."
        ),
    ))

    strong = [g for g in gaps if g["strength"] == "strong"]
    moderate = [g for g in gaps if g["strength"] == "moderate"]

    if checked == 0 or total == 0:
        positioning = (
            "Competitor website analysis did not run for this area — "
            "differentiation gaps cannot be evidenced yet."
        )
    elif strong:
        labels = [g["label"].lower() for g in strong[:2]]
        positioning = (
            "Strong differentiation room around " + " and ".join(labels) + "."
        )
    elif moderate:
        labels = [g["label"].lower() for g in moderate[:2]]
        positioning = (
            "Moderate differentiation room around " + " and ".join(labels) + "."
        )
    else:
        positioning = (
            "Local competitors cover the basics — differentiate on price, "
            "partnerships, or speed-to-certification."
        )

    data_confidence = _confidence_from_checked(checked, total)
    return {
        "gaps": gaps,
        "positioning": positioning,
        "data_confidence": data_confidence,
        "checked_count": checked,
        "total_competitors": total,
    }
