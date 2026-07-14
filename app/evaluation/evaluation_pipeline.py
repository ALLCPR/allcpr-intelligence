"""
Evaluation pipeline (Phase 5) — the orchestrator.

Adapts the already-built ``course_performance`` payload (the dict produced by
``app.reports.interpretation.build_course_performance_section``) plus area-level
public-demand and competition signals into one course opportunity graph per
course type, then groups the courses into Primary / Secondary / Avoid-or-test
and writes a deterministic summary.

The output is a plain, JSON-serializable dict, ready to attach to the
``course_performance`` block (so it flows into the report JSON) and to render in
the HTML / Markdown reports.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.evaluation.explanation_engine import (
    explain_course_result,
    summarize_primary_secondary_avoid,
)
from app.evaluation.score_graph import ScoreGraphResult, build_course_score_graph


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _history_confidence(total_classes: Optional[float]) -> str:
    """Coarse confidence label from class count (mirrors forecasting bands)."""
    n = total_classes or 0
    if n >= 12:
        return "high"
    if n >= 5:
        return "medium"
    if n >= 1:
        return "low"
    return "none"


def _forecast_for(forecast_block: Optional[Dict[str, Any]],
                  course_type: str,
                  reference_avg: Optional[float]) -> Optional[Dict[str, Any]]:
    if not forecast_block:
        return None
    for entry in forecast_block.get("course_types") or []:
        if entry.get("course_type") == course_type:
            return {
                "expected_students": entry.get("expected_students"),
                "confidence": entry.get("confidence"),
                "reference_avg": reference_avg,
            }
    return None


def build_evaluation_graph(
    course_performance: Optional[Dict[str, Any]],
    demand: Optional[Dict[str, Any]] = None,
    competition: Optional[Dict[str, Any]] = None,
    allcpr_overall_avg: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Build the course opportunity graph for an area.

    Returns ``None`` when there is no course data (so the caller can fall back
    to the existing strategy logic).
    """
    if not course_performance:
        return None
    course_types = course_performance.get("course_types") or []
    if not course_types:
        return None

    overall = course_performance.get("overall") or {}
    reference_avg = allcpr_overall_avg or _num(overall.get("average_students_per_class"))
    schedule = course_performance.get("schedule_intelligence")
    forecast_block = course_performance.get("forecast")

    results: List[ScoreGraphResult] = []
    for ct in course_types:
        course_type = ct.get("course_type")
        label = ct.get("label") or course_type
        total_classes = _num(ct.get("total_classes"))
        local_avg = _num(ct.get("average_students_per_class"))

        historical = {
            "score": ct.get("course_performance_score"),
            "average_students_per_class": local_avg,
            "total_classes": total_classes,
            "fill_rate_percent": ct.get("fill_rate_percent"),
            "confidence": _history_confidence(total_classes),
        }
        course_relative = (
            {"local_avg": local_avg, "allcpr_avg": reference_avg}
            if local_avg is not None and reference_avg else None
        )
        forecast = _forecast_for(forecast_block, course_type, reference_avg)

        results.append(build_course_score_graph(
            course_type=course_type,
            label=label,
            historical=historical,
            course_relative=course_relative,
            demand=demand,
            competition=competition,
            schedule=schedule,
            forecast=forecast,
        ))

    results.sort(key=lambda r: r.final_score, reverse=True)

    primary, secondary, avoid = [], [], []
    confidence_notes: List[str] = []
    for r in results:
        if r.recommendation.display_group == "Primary":
            primary.append(r.label)
        elif r.recommendation.display_group == "Secondary":
            secondary.append(r.label)
        else:
            avoid.append(r.label)
        if r.penalty.confidence_level in ("low", "very_low"):
            confidence_notes.append(
                f"{r.label}: {r.penalty.confidence_level.replace('_', '-')} "
                f"confidence — {r.penalty.reasons[0] if r.penalty.reasons else 'thin evidence.'}"
            )

    return {
        "course_opportunity_graph": [r.to_dict() for r in results],
        "primary": primary,
        "secondary": secondary,
        "avoid_or_test": avoid,
        "summary": summarize_primary_secondary_avoid(results),
        "explanations": [explain_course_result(r) for r in results],
        "confidence_notes": confidence_notes,
    }
