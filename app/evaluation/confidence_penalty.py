"""
Confidence penalty (Phase 5).

Deterministic logic that lowers our stated confidence in a course
recommendation when the evidence is thin, incomplete, or stale. The penalty is
expressed in *score points* subtracted from the final course opportunity score,
plus a coarse ``confidence_level`` label and plain-English ``reasons``.

Design principle (mirrors the rest of the codebase): never fabricate certainty.
A small/absent field reduces confidence; it does not invent a value. Missing the
*entire* history reduces confidence strongly but predictably — it never throws
an exception or pretends the data exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# Sample-size penalty bands. Keys are inclusive lower bounds.
_NO_HISTORY_PENALTY = 35.0
_TINY_SAMPLE_PENALTY = 20.0       # n < 10
_SMALL_SAMPLE_PENALTY = 10.0      # 10 <= n < 30
_MODERATE_SAMPLE_PENALTY = 4.0    # 30 <= n < 100
# n >= 100 -> 0

_MISSING_FILL_PENALTY = 2.0
_MISSING_FORECAST_PENALTY = 2.0
_MISSING_GENERIC_PENALTY = 1.0

_AGING_DAYS = 365
_STALE_DAYS = 730
_AGING_PENALTY = 3.0
_STALE_PENALTY = 6.0

# Total penalty -> confidence level.
_LEVEL_BANDS = (
    (4.0, "high"),
    (12.0, "medium"),
    (25.0, "low"),
)


def _level_for(points: float) -> str:
    for ceiling, label in _LEVEL_BANDS:
        if points <= ceiling:
            return label
    return "very_low"


def _effective_n(sample_size: Optional[int], class_count: Optional[int]) -> Optional[int]:
    for candidate in (sample_size, class_count):
        if candidate is not None:
            return int(candidate)
    return None


@dataclass
class ConfidencePenalty:
    penalty_points: float
    confidence_level: str
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "penalty_points": self.penalty_points,
            "confidence_level": self.confidence_level,
            "reasons": list(self.reasons),
        }


def compute_confidence_penalty(
    sample_size: Optional[int] = None,
    historical_confidence: Optional[Union[str, float]] = None,
    forecast_confidence: Optional[Union[str, float]] = None,
    data_freshness_days: Optional[int] = None,
    missing_fields: Optional[List[str]] = None,
    class_count: Optional[int] = None,
    has_history: bool = True,
) -> ConfidencePenalty:
    """Compute the deterministic confidence penalty for a course recommendation.

    Returns a :class:`ConfidencePenalty` with the points to subtract, a coarse
    confidence level, and the reasons behind it.
    """
    reasons: List[str] = []
    points = 0.0

    missing_fields = [str(f).lower() for f in (missing_fields or [])]
    n = _effective_n(sample_size, class_count)
    no_history = (
        not has_history
        or n is None
        or n == 0
        # Only an *explicit* "none" status means no history; an unset
        # (``None``) argument means "not provided", not "no history".
        or (historical_confidence is not None
            and str(historical_confidence).lower() == "none")
    )

    # 1. Sample-size / history availability.
    if no_history:
        points += _NO_HISTORY_PENALTY
        reasons.append(
            "No matching ALLCPR class history — recommendation rests on public "
            "signals only, so confidence is strongly reduced."
        )
    elif n < 10:
        points += _TINY_SAMPLE_PENALTY
        reasons.append(
            f"Very small sample ({n} class(es)) — one or two classes can swing "
            "the average, so confidence is low."
        )
    elif n < 30:
        points += _SMALL_SAMPLE_PENALTY
        reasons.append(
            f"Small sample ({n} classes) — enough to suggest a direction, not "
            "to be sure."
        )
    elif n < 100:
        points += _MODERATE_SAMPLE_PENALTY
        reasons.append(
            f"Moderate sample ({n} classes) — a reasonable but not large base."
        )

    # 2. Missing soft fields (do not destroy the score).
    if "fill_rate" in missing_fields or "fill_rate_percent" in missing_fields:
        points += _MISSING_FILL_PENALTY
        reasons.append(
            "Fill-rate/capacity not in the export — capacity utilization is "
            "unknown (not assumed)."
        )
    if "forecast" in missing_fields:
        points += _MISSING_FORECAST_PENALTY
        reasons.append("No forecast signal available for this course.")
    for fld in missing_fields:
        if fld in ("fill_rate", "fill_rate_percent", "forecast"):
            continue
        points += _MISSING_GENERIC_PENALTY
        reasons.append(f"Missing field '{fld}' — left unknown, not imputed.")

    # 3. Stale history — only when date fields actually exist.
    if data_freshness_days is not None:
        if data_freshness_days >= _STALE_DAYS:
            points += _STALE_PENALTY
            reasons.append(
                f"History is stale (latest class ~{data_freshness_days} days "
                "old) — local conditions may have changed."
            )
        elif data_freshness_days >= _AGING_DAYS:
            points += _AGING_PENALTY
            reasons.append(
                f"History is aging (latest class ~{data_freshness_days} days "
                "old)."
            )

    # 4. Weak forecast confidence is a mild signal on top of everything else.
    if str(forecast_confidence).lower() == "low" and not no_history:
        points += _MISSING_GENERIC_PENALTY
        reasons.append("Forecast confidence is low for this course.")

    points = round(points, 2)
    return ConfidencePenalty(
        penalty_points=points,
        confidence_level=_level_for(points),
        reasons=reasons,
    )
