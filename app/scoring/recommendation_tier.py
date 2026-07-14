"""
Recommendation tier — A / B / C / D / F.

Rules (in priority order):
  - F: site_score < 30                       (avoid)
  - D: site_score 30..49                     (not recommended)
  - Confidence < 30                          => cap at C
  - Effective saturation >= 0.9 AND gap < 25 => cap at C
  - C: site_score 50..64 OR capped above
  - B: site_score 65..79                     (promising, validate)
  - A: site_score >= 80                      (strong)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


TIER_LABELS = {
    "A": "Strong candidate",
    "B": "Promising — validate",
    "C": "Mixed / needs more data",
    "D": "Not recommended",
    "F": "Avoid",
}


@dataclass
class TierVerdict:
    tier: str
    label: str
    reasons: List[str]
    executive_state: str = "Recommended for field validation"


def _base_tier_from_score(site_score: float) -> str:
    if site_score < 30:
        return "F"
    if site_score < 50:
        return "D"
    if site_score < 65:
        return "C"
    if site_score < 80:
        return "B"
    return "A"


def _cap_tier(tier: str, max_tier: str) -> str:
    order = ["F", "D", "C", "B", "A"]
    return order[min(order.index(tier), order.index(max_tier))]


def _executive_state(
    area_score: float,
    candidate_type: str,
    site_validated: bool,
) -> str:
    """Honest top-line recommendation, distinct from the A–F tier.

    A great *area* never reads as lease-ready unless a commercial space has
    actually been validated.
    """
    if site_validated:
        return "Lease-ready candidate"
    if candidate_type == "invalid_or_low_confidence" or area_score < 45:
        return "Not recommended"
    if candidate_type == "commercial_area_proxy" and area_score >= 55:
        # Good area with a commercial signal — go find/validate a real space.
        return "Recommended for listing search"
    return "Recommended for field validation"


def compute_tier(
    site_score: float,
    confidence_score: float,
    effective_saturation: float,
    competition_gap_score: float,
    candidate_type: str = "landmark_proxy",
    site_validated: bool = False,
) -> TierVerdict:
    """``site_score`` here is the AREA score (ranking number). The gated,
    space-level site_score lives separately on the scored dict."""
    area_score = site_score
    tier = _base_tier_from_score(area_score)
    reasons: List[str] = [f"area_score={area_score:.1f} -> base tier {tier}"]

    if confidence_score < 30:
        capped = _cap_tier(tier, "C")
        if capped != tier:
            reasons.append(
                f"confidence={confidence_score:.1f} < 30 — capped at C"
            )
        tier = capped

    if effective_saturation >= 0.9 and competition_gap_score < 25:
        capped = _cap_tier(tier, "C")
        if capped != tier:
            reasons.append(
                f"market is saturated (saturation={effective_saturation:.2f}, "
                f"gap={competition_gap_score:.1f}) — capped at C"
            )
        tier = capped

    # An unvalidated space caps the tier at B — never "A / strong" on demand
    # alone, because we can't confirm a leasable site.
    if not site_validated and tier == "A":
        tier = _cap_tier(tier, "B")
        reasons.append("no validated commercial space — capped at B (area-level)")

    state = _executive_state(area_score, candidate_type, site_validated)
    return TierVerdict(tier=tier, label=TIER_LABELS[tier], reasons=reasons,
                       executive_state=state)
