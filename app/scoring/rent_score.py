"""Commercial rent override scoring.

Rent data is optional and must come from explicit overrides or future cited
sources. Unknown rent stays unknown and does not affect the weighted site score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class RentBreakdown:
    rent_score: Optional[float]
    rent_data_confidence: str
    rent_source: str
    rent_notes: str


def compute_rent_score(economy_block: Dict[str, object]) -> RentBreakdown:
    real_estate = economy_block.get("real_estate") or {}
    values = real_estate.get("values") or {}
    rent = values.get("rent_per_sqft_annual")
    source = values.get("rent_source") or ""
    notes = values.get("rent_notes") or ""
    confidence = values.get("rent_data_confidence") or "unknown"

    if not isinstance(rent, (int, float)):
        return RentBreakdown(
            rent_score=None,
            rent_data_confidence=str(confidence or "unknown"),
            rent_source=str(source or ""),
            rent_notes=str(notes or "No rent override matched; rent is unknown."),
        )

    # Heuristic: lower annual rent improves affordability. This is reported as
    # a separate signal, not part of the final site_score weights.
    low = 18.0
    high = 72.0
    score = max(0.0, min(1.0, (high - float(rent)) / (high - low))) * 100.0
    return RentBreakdown(
        rent_score=round(score, 2),
        rent_data_confidence=str(confidence or "manual_override"),
        rent_source=str(source or ""),
        rent_notes=str(notes or ""),
    )
