"""
Candidate-type classification.

Turns the raw ``viability`` signal (from ``enrichers/area_profile``) plus any
matched commercial-listing override into an honest label that drives whether a
candidate is presented as a *site* or only an *area*:

  - ``verified_commercial_listing`` — a human-validated leasable space exists
    (override row with validation_status == validated). Unlocks site_score.
  - ``commercial_area_proxy``       — anchor carries a commercial signal
    (storefront / office / retail), so the *area* looks leasable, but no
    specific space is validated.
  - ``landmark_proxy``              — viable coordinate near a landmark with no
    commercial signal (today's "needs commercial site validation" case).
  - ``invalid_or_low_confidence``   — not a viable anchor (transit / spam / none).

Also reports ``demand_validation_level``: how strongly demand is confirmed —
``proxy`` (collected signals only) | ``tested`` (a real ads/landing test was
run) | ``confirmed`` (real enrollment outcome). Without field data it stays
``proxy``; the manual override / backtest paths can raise it later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

VERIFIED_COMMERCIAL_LISTING = "verified_commercial_listing"
COMMERCIAL_AREA_PROXY = "commercial_area_proxy"
LANDMARK_PROXY = "landmark_proxy"
INVALID_OR_LOW_CONFIDENCE = "invalid_or_low_confidence"

# Human-readable label + whether this type may carry a real site_score.
TYPE_LABELS = {
    VERIFIED_COMMERCIAL_LISTING: "Verified commercial listing",
    COMMERCIAL_AREA_PROXY: "Commercial area (proxy)",
    LANDMARK_PROXY: "Area-level candidate (landmark proxy)",
    INVALID_OR_LOW_CONFIDENCE: "Invalid / low confidence",
}


@dataclass
class CandidateTypeResult:
    candidate_type: str
    label: str
    is_site_candidate: bool          # True only for verified_commercial_listing
    demand_validation_level: str     # proxy | tested | confirmed
    reason: str


def classify(
    profile: Dict[str, object],
    override: Optional[object] = None,
) -> CandidateTypeResult:
    """Classify a candidate from its viability dict + optional override.

    ``override`` is a ``commercial_listings.CommercialOverride`` or None.
    """
    viability: Dict[str, object] = profile.get("viability") or {}  # type: ignore[assignment]
    viable = bool(viability.get("viable", True))
    commercial_anchor = bool(viability.get("commercial_anchor", False))

    # Demand is proxy-only unless an override explicitly records a field test.
    demand_level = "proxy"
    if override is not None:
        notes = f"{getattr(override, 'broker_notes', '')} {getattr(override, 'parking_notes', '')}".lower()
        if "enrollment" in notes or "confirmed" in notes:
            demand_level = "confirmed"
        elif "ads test" in notes or "landing test" in notes or "tested" in notes:
            demand_level = "tested"

    if not viable:
        return CandidateTypeResult(
            candidate_type=INVALID_OR_LOW_CONFIDENCE,
            label=TYPE_LABELS[INVALID_OR_LOW_CONFIDENCE],
            is_site_candidate=False,
            demand_validation_level=demand_level,
            reason=str(viability.get("reason") or "anchor not viable"),
        )

    if override is not None and getattr(override, "is_validated", False):
        return CandidateTypeResult(
            candidate_type=VERIFIED_COMMERCIAL_LISTING,
            label=TYPE_LABELS[VERIFIED_COMMERCIAL_LISTING],
            is_site_candidate=True,
            demand_validation_level=demand_level,
            reason="validated commercial-listing override matched",
        )

    if commercial_anchor:
        return CandidateTypeResult(
            candidate_type=COMMERCIAL_AREA_PROXY,
            label=TYPE_LABELS[COMMERCIAL_AREA_PROXY],
            is_site_candidate=False,
            demand_validation_level=demand_level,
            reason=str(viability.get("commercial_reason")
                       or "anchor carries a commercial signal"),
        )

    return CandidateTypeResult(
        candidate_type=LANDMARK_PROXY,
        label=TYPE_LABELS[LANDMARK_PROXY],
        is_site_candidate=False,
        demand_validation_level=demand_level,
        reason="viable coordinate near a landmark, no commercial signal",
    )
