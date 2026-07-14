"""
Backtest simulator (Phase 5) — a deliberate placeholder.

We cannot yet backtest future ads, future demand, or unknown future students.
This module is NOT machine learning. It only defines the small, honest
structures we will later use to ask: *"When the graph said EXPAND, did actual
enrollment beat the success threshold?"* Until real outcomes are recorded,
``success`` stays ``None`` (unevaluable) rather than pretending to know.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def evaluate_prediction(
    predicted_score: float,
    actual_students: Optional[float],
    success_threshold: float,
) -> Optional[bool]:
    """Did the real outcome beat the threshold?

    Returns ``True``/``False`` once a real ``actual_students`` is known, or
    ``None`` when the outcome has not been observed yet (the common case today).
    ``predicted_score`` is accepted for future calibration analysis but does not
    decide success on its own.
    """
    if actual_students is None:
        return None
    return float(actual_students) >= float(success_threshold)


@dataclass
class BacktestScenario:
    area: str
    course_type: str
    predicted_score: float
    actual_students: Optional[float] = None
    success_threshold: float = 6.0
    success: Optional[bool] = None

    def __post_init__(self) -> None:
        # Derive success from the outcome unless one was set explicitly.
        if self.success is None:
            self.success = evaluate_prediction(
                self.predicted_score, self.actual_students, self.success_threshold
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "area": self.area,
            "course_type": self.course_type,
            "predicted_score": self.predicted_score,
            "actual_students": self.actual_students,
            "success_threshold": self.success_threshold,
            "success": self.success,
        }
