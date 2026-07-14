"""
Business-feasibility scoring — "is this a workable training-center space?"

Distinct from area demand: this grades the *space* on rent affordability,
parking, classroom fit, access, visibility, and overall lease-readiness, then
estimates the unit economics (break-even students, revenue/cost bands, risk).

Honesty rules:
- Space-level sub-scores (parking / classroom_fit / visibility) come ONLY from a
  validated commercial override. Without one they are ``None`` ("not validated"),
  never guessed.
- ``access_score`` is the collected accessibility proxy (always available).
- ``rent_score`` prefers the override's cited asking_rent; otherwise falls back
  to the rent-pressure estimate (clearly a proxy).
- ``lease_readiness_score`` is only computed when an override exists — it's the
  signal that a *site* (not just an *area*) has been examined.
- Break-even / revenue / cost are ESTIMATES from config assumptions + the real
  per-state ALLCPR price (``allcpr_prices``).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.collectors import allcpr_prices
from app.config import (
    CLASSROOM_TARGET_STUDENTS,
    FIXED_COST_MONTHLY_HIGH,
    FIXED_COST_MONTHLY_LOW,
    SQFT_PER_STUDENT,
    VARIABLE_COST_PER_STUDENT,
)

# Rent affordability scaling (matches rent_score.py): $18/sqft → 100, $72 → 0.
_RENT_LOW, _RENT_HIGH = 18.0, 72.0


@dataclass
class FeasibilityBreakdown:
    rent_score: Optional[float]
    parking_score: Optional[float]
    classroom_fit_score: Optional[float]
    access_score: Optional[float]
    visibility_score: Optional[float]
    lease_readiness_score: Optional[float]
    breakeven_students_per_month: Optional[int]
    monthly_revenue_range: Tuple[Optional[float], Optional[float]]
    monthly_fixed_cost_range: Tuple[float, float]
    risk_level: str                       # Low | Medium | High
    data_basis: str                       # validated_override | proxy | none
    rationale: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rent_score": self.rent_score,
            "parking_score": self.parking_score,
            "classroom_fit_score": self.classroom_fit_score,
            "access_score": self.access_score,
            "visibility_score": self.visibility_score,
            "lease_readiness_score": self.lease_readiness_score,
            "breakeven_students_per_month": self.breakeven_students_per_month,
            "monthly_revenue_range": list(self.monthly_revenue_range),
            "monthly_fixed_cost_range": list(self.monthly_fixed_cost_range),
            "risk_level": self.risk_level,
            "data_basis": self.data_basis,
            "rationale": self.rationale,
        }


def _rent_affordability(rent_per_sqft: Optional[float]) -> Optional[float]:
    if not isinstance(rent_per_sqft, (int, float)):
        return None
    score = (_RENT_HIGH - float(rent_per_sqft)) / (_RENT_HIGH - _RENT_LOW)
    return round(max(0.0, min(1.0, score)) * 100.0, 2)


def _parking_from_notes(notes: str) -> Optional[float]:
    """Heuristic 0..100 parking score from free-text broker/parking notes."""
    if not notes:
        return None
    n = notes.lower()
    if any(w in n for w in ("no parking", "street only", "no dedicated")):
        return 30.0
    score = 55.0
    if "dedicated" in n or "reserved" in n:
        score += 20
    if "lot" in n or "garage" in n or "spaces" in n:
        score += 15
    nums = [int(s) for s in re.findall(r"\b(\d{1,3})\b", n)]
    if nums and max(nums) >= 10:
        score += 10
    return round(min(100.0, score), 2)


def _visibility_from_notes(notes: str) -> Optional[float]:
    if not notes:
        return None
    n = notes.lower()
    score = 50.0
    if any(w in n for w in ("ground floor", "ground-floor", "street-facing",
                            "corner", "high visibility", "signage")):
        score += 25
    if any(w in n for w in ("second floor", "rear", "back of", "basement",
                            "low visibility", "no signage")):
        score -= 25
    return round(max(0.0, min(100.0, score)), 2)


def _classroom_fit(square_feet: Optional[float]) -> Optional[float]:
    """Score how well the suite fits a target class size (config-driven)."""
    if not isinstance(square_feet, (int, float)) or square_feet <= 0:
        return None
    capacity = square_feet / max(1.0, SQFT_PER_STUDENT)
    ratio = capacity / max(1.0, float(CLASSROOM_TARGET_STUDENTS))
    # 1.0x target → 100; below scales down; comfortably above stays high.
    score = min(1.0, ratio) * 100.0 if ratio < 1.0 else min(100.0, 90.0 + ratio * 5.0)
    return round(min(100.0, score), 2)


def _risk_level(
    *,
    competition_pressure_score: Optional[float],
    confidence_score: float,
    area_score: float,
    has_validated_site: bool,
) -> str:
    points = 0
    if competition_pressure_score is not None and competition_pressure_score >= 60:
        points += 1
    if confidence_score < 50:
        points += 1
    if area_score < 50:
        points += 1
    if not has_validated_site:
        points += 1  # no validated space is itself a risk
    if points >= 3:
        return "High"
    if points >= 1:
        return "Medium"
    return "Low"


def compute_feasibility(
    *,
    override: Optional[object],
    state: str,
    accessibility_score: float,
    confidence_score: float,
    competition_pressure_score: Optional[float],
    area_score: float,
    revenue_low: Optional[float],
    revenue_high: Optional[float],
) -> FeasibilityBreakdown:
    has_override = override is not None
    validated = bool(has_override and getattr(override, "is_validated", False))

    asking_rent = getattr(override, "asking_rent", None) if has_override else None
    square_feet = getattr(override, "square_feet", None) if has_override else None
    parking_notes = (getattr(override, "parking_notes", "") or "") if has_override else ""
    broker_notes = (getattr(override, "broker_notes", "") or "") if has_override else ""

    rent_score = _rent_affordability(asking_rent)
    parking_score = _parking_from_notes(parking_notes)
    classroom_fit_score = _classroom_fit(square_feet)
    visibility_score = _visibility_from_notes(broker_notes + " " + parking_notes)
    access_score = round(float(accessibility_score), 2)  # proxy, always available

    # Lease-readiness only when a real space has been examined (override present).
    lease_readiness_score: Optional[float] = None
    if has_override:
        parts = [s for s in (rent_score, parking_score, classroom_fit_score,
                             access_score, visibility_score) if s is not None]
        if parts:
            lease_readiness_score = round(sum(parts) / len(parts), 2)

    # Break-even: fixed-cost midpoint / per-student contribution margin.
    avg_price = allcpr_prices.lookup_price(state).avg_price
    contribution = avg_price - VARIABLE_COST_PER_STUDENT
    fixed_mid = (FIXED_COST_MONTHLY_LOW + FIXED_COST_MONTHLY_HIGH) / 2.0
    breakeven: Optional[int] = None
    if contribution > 0:
        breakeven = int(math.ceil(fixed_mid / contribution))

    risk = _risk_level(
        competition_pressure_score=competition_pressure_score,
        confidence_score=confidence_score,
        area_score=area_score,
        has_validated_site=validated,
    )

    basis = "validated_override" if validated else ("proxy" if has_override else "none")

    rationale: List[str] = []
    if breakeven is not None:
        rationale.append(
            f"break-even ≈ {breakeven} students/mo at ${avg_price:.0f}/student "
            f"(− ${VARIABLE_COST_PER_STUDENT:.0f} variable) vs "
            f"${fixed_mid:,.0f}/mo fixed (estimated)"
        )
    if rent_score is not None:
        rationale.append(f"rent ${asking_rent:.0f}/sqft/yr → affordability {rent_score:.0f}/100 (cited)")
    elif not has_override:
        rationale.append("no validated commercial space — site-level scores withheld")
    if not validated:
        rationale.append("space not validated: this is an area read, not a lease-ready site")

    return FeasibilityBreakdown(
        rent_score=rent_score,
        parking_score=parking_score,
        classroom_fit_score=classroom_fit_score,
        access_score=access_score,
        visibility_score=visibility_score,
        lease_readiness_score=lease_readiness_score,
        breakeven_students_per_month=breakeven,
        monthly_revenue_range=(revenue_low, revenue_high),
        monthly_fixed_cost_range=(FIXED_COST_MONTHLY_LOW, FIXED_COST_MONTHLY_HIGH),
        risk_level=risk,
        data_basis=basis,
        rationale=rationale,
    )
