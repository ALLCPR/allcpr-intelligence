"""
Calibration helpers comparing modeled demand to proven ALLCPR demand.

The comparison is only defined where a modeled ZIP also has real historical
ALLCPR outcomes. ZIPs without history receive an explicit no-history status;
they do not inherit or blend proven demand from anywhere else.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.scoring.historical_proven_demand import compute_proven_demand_score

HIGH_THRESHOLD = 65.0
LOW_THRESHOLD = 45.0


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _band(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value >= HIGH_THRESHOLD:
        return "high"
    if value >= LOW_THRESHOLD:
        return "medium"
    return "low"


def compute_model_error(modeled_score: float, proven_score: float) -> float:
    """Positive means history outperformed the public model."""
    return round(float(proven_score) - float(modeled_score), 1)


def classify_model_agreement(modeled_score: float, proven_score: float) -> str:
    """Classify modeled vs proven demand using high/medium/low bands."""
    modeled_band = _band(_num(modeled_score))
    proven_band = _band(_num(proven_score))
    if modeled_band == "high" and proven_band == "high":
        return "model_agrees_high"
    if modeled_band == "low" and proven_band == "low":
        return "model_agrees_low"
    if modeled_band == "high" and proven_band == "low":
        return "model_overpredicts"
    if modeled_band == "low" and proven_band == "high":
        return "model_underpredicts"
    if modeled_band == "medium" and proven_band == "high":
        return "hidden_opportunity"
    if modeled_band == "high" and proven_band in {"medium", "low"}:
        return "test_carefully"
    return "mixed"


def _note(agreement: str, confidence: str) -> str:
    if confidence == "low":
        return "History sample is small; treat as directional only."
    return {
        "model_agrees_high": "Model agrees with strong historical performance.",
        "model_agrees_low": "Model agrees with weak historical performance.",
        "model_overpredicts": (
            "Public model may be overpredicting this ZIP relative to actual ALLCPR history."
        ),
        "model_underpredicts": (
            "Historical student performance is stronger than the public model suggests."
        ),
        "hidden_opportunity": (
            "Proven demand is strong despite only moderate public-data signals."
        ),
        "test_carefully": (
            "Modeled demand is strong, but proven ALLCPR performance is not equally strong."
        ),
    }.get(agreement, "Modeled and proven demand are mixed; keep this on the automated watchlist.")


def compare_modeled_vs_proven(
    modeled_row: Dict[str, Any],
    historical_row: Dict[str, Any],
) -> Dict[str, Any]:
    """Return side-by-side proven demand and calibration fields."""
    proven = compute_proven_demand_score(historical_row)
    modeled = _num(modeled_row.get("overall"))
    proven_score = _num(proven.get("proven_demand_score"))
    confidence = str(proven.get("historical_confidence") or "low")
    if modeled is None or proven_score is None:
        agreement = "insufficient_history"
        error = None
    elif confidence == "low":
        agreement = "insufficient_history"
        error = compute_model_error(modeled, proven_score)
    else:
        agreement = classify_model_agreement(modeled, proven_score)
        error = compute_model_error(modeled, proven_score)
    return {
        **proven,
        "historical_status": "has_allcpr_history",
        "modeled_overall_score": modeled,
        "proven_class_count": _num(
            historical_row.get("class_count") or historical_row.get("classes")),
        "proven_avg_students": _num(
            historical_row.get("avg_students")
            or historical_row.get("average_students_per_class")),
        "proven_fill_rate": _num(historical_row.get("fill_rate")),
        "model_error": error,
        "model_agreement": agreement,
        "calibration_note": _note(agreement, confidence),
    }
