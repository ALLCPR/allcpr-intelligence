"""
Course opportunity score graph (Phase 5).

Builds the deterministic, explainable score for ONE course type in one area
from flexible evidence dictionaries produced upstream (course performance,
public demand, competition, schedule intelligence, forecast). Each piece of
evidence becomes a :class:`ScoreNode`; the nodes are weighted, missing nodes
drop out and the remaining weights renormalize, and a confidence penalty is
subtracted at the end.

Honesty rules (shared with the rest of the codebase):
  - A missing signal is recorded as a node with ``value=None, missing=True`` and
    contributes nothing — it is never fabricated into a number.
  - Missing evidence lowers *confidence* (via renormalization + penalty); it
    does not silently inflate the score by being treated as zero.
  - ``Σ present-node contributions − penalty == final_score`` (clamped 0..100),
    so every score is auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.evaluation.confidence_penalty import (
    ConfidencePenalty,
    compute_confidence_penalty,
)
from app.evaluation.course_recommendation import (
    CourseRecommendation,
    recommend_course,
)
from app.evaluation.score_node import ScoreNode

# Default importance weights (must sum to 1.0 before renormalization).
DEFAULT_WEIGHTS: Dict[str, float] = {
    "historical_performance": 0.35,
    "public_demand": 0.20,
    "course_relative_performance": 0.15,
    "competition_gap": 0.10,
    "schedule_strength": 0.10,
    "forecast_expected_students": 0.10,
}

_NODE_LABELS = {
    "historical_performance": "Historical enrollment",
    "public_demand": "Public demand",
    "course_relative_performance": "Course vs ALLCPR average",
    "competition_gap": "Competition gap",
    "schedule_strength": "Schedule strength",
    "forecast_expected_students": "Forecast expected students",
}

# String confidence -> numeric (0..1).
_CONF_MAP = {"high": 0.9, "medium": 0.7, "low": 0.45, "none": 0.1}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _conf(value: Any, default: float = 0.6) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return _CONF_MAP.get(str(value).lower(), default)


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _ratio_subscore(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    """Map a value vs a reference onto 0..100 (parity = 50, double = 100)."""
    if value is None or not reference:
        return None
    return _clamp(50.0 * (value / reference))


def _avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


@dataclass
class ScoreGraphResult:
    course_type: str
    label: str
    final_score: float
    recommendation: CourseRecommendation
    nodes: List[ScoreNode]
    penalty: ConfidencePenalty
    reasons: List[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "course_type": self.course_type,
            "label": self.label,
            "final_score": self.final_score,
            "recommendation": self.recommendation.action,
            "display_group": self.recommendation.display_group,
            "recommendation_label": self.recommendation.label,
            "confidence": self.confidence,
            "nodes": [n.to_dict() for n in self.nodes],
            "penalty": self.penalty.to_dict(),
            "reasons": list(self.reasons),
        }


# --- per-component evidence extraction -------------------------------------
# Each returns (subscore_0_100 | None, value | None, confidence_0_1, reasons).

def _historical_evidence(historical: Optional[Dict[str, Any]]):
    if not historical:
        return None, None, 0.0, ["No matching ALLCPR history for this course here."]
    avg = _num(historical.get("average_students_per_class"))
    score = _num(historical.get("score"))
    reasons: List[str] = []
    if score is not None:
        subscore = _clamp(score)
        reasons.append(f"Course performance score {score:.0f}/100 from history.")
        if avg is not None:
            reasons.append(f"Averages {avg:.2f} students/class.")
    elif avg is not None:
        subscore = _ratio_subscore(avg, historical.get("allcpr_avg") or 6.0)
        reasons.append(f"Averages {avg:.2f} students/class historically.")
    else:
        return None, None, 0.0, ["History exists but carries no enrollment counts."]
    conf = _conf(historical.get("confidence"))
    return subscore, avg if avg is not None else score, conf, reasons


def _relative_evidence(course_relative: Optional[Dict[str, Any]]):
    if not course_relative:
        return None, None, 0.0, ["No ALLCPR-wide average to compare against."]
    local = _num(course_relative.get("local_avg"))
    overall = _num(course_relative.get("allcpr_avg"))
    subscore = _ratio_subscore(local, overall)
    if subscore is None:
        return None, None, 0.0, ["Not enough data to compare to the ALLCPR average."]
    rel_pct = round(100.0 * (local / overall - 1.0), 1) if overall else None
    direction = "above" if subscore >= 50 else "below"
    reasons = [
        f"Local average {local:.2f} is {direction} the ALLCPR average "
        f"{overall:.2f} ({rel_pct:+.0f}%)." if rel_pct is not None else
        f"Local average {local:.2f} vs ALLCPR average {overall:.2f}."
    ]
    return subscore, rel_pct, 0.6, reasons


def _demand_evidence(demand: Optional[Dict[str, Any]]):
    if not demand:
        return None, None, 0.0, ["No public demand signal supplied for this area."]
    parts = []
    for key in ("demand_score", "healthcare_training_ecosystem_score",
                "job_certification_demand_score"):
        v = _num(demand.get(key))
        if v is not None:
            parts.append(_clamp(v))
    combined = _avg(parts)
    if combined is None:
        return None, None, 0.0, ["No usable public demand sub-scores."]
    reasons = [
        f"Public demand signals average {combined:.0f}/100 "
        f"({len(parts)} signal(s))."
    ]
    return combined, round(combined, 1), 0.6, reasons


def _competition_evidence(competition: Optional[Dict[str, Any]]):
    if not competition:
        return None, None, 0.0, ["No competition signal supplied for this area."]
    gap = _num(competition.get("competition_gap_score"))
    if gap is None:
        return None, None, 0.0, ["Competition gap score is unknown here."]
    subscore = _clamp(gap)
    desc = "an open market" if subscore >= 60 else (
        "a contested market" if subscore < 40 else "a moderately served market")
    reasons = [f"Competition gap {gap:.0f}/100 — {desc}."]
    return subscore, round(gap, 1), 0.55, reasons


def _schedule_evidence(schedule: Optional[Dict[str, Any]]):
    if not schedule:
        return None, None, 0.0, ["No schedule signal learned from history."]
    strength = _num(schedule.get("strength"))
    if strength is not None:
        subscore = _clamp(strength)
        return subscore, round(strength, 1), 0.5, [
            f"Schedule strength {strength:.0f}/100 from class history."]
    # Derive a coarse strength from a schedule_intelligence payload.
    best_day = schedule.get("best_day") or {}
    if best_day.get("basis") == "enrollment":
        return 62.0, None, 0.5, [
            f"Best day {best_day.get('label')} is set by attendance, not just "
            "volume — a usable scheduling signal."]
    if best_day:
        return 50.0, None, 0.35, [
            "Scheduling is known by volume only (enrollment not recorded)."]
    return None, None, 0.0, ["No dated history to learn a schedule from."]


def _forecast_evidence(forecast: Optional[Dict[str, Any]]):
    if not forecast:
        return None, None, 0.0, ["No forecast available for this course."]
    expected = _num(forecast.get("expected_students"))
    if expected is None:
        return None, None, 0.0, ["Forecast has no expected-students figure."]
    subscore = _ratio_subscore(expected, forecast.get("reference_avg") or 6.0)
    conf = _conf(forecast.get("confidence"), default=0.45)
    reasons = [f"Recency-weighted forecast ~{expected:.1f} students/class."]
    return subscore, round(expected, 2), conf, reasons


_EXTRACTORS = {
    "historical_performance": _historical_evidence,
    "public_demand": _demand_evidence,
    "course_relative_performance": _relative_evidence,
    "competition_gap": _competition_evidence,
    "schedule_strength": _schedule_evidence,
    "forecast_expected_students": _forecast_evidence,
}


def build_course_score_graph(
    course_type: str,
    label: Optional[str] = None,
    historical: Optional[Dict[str, Any]] = None,
    course_relative: Optional[Dict[str, Any]] = None,
    demand: Optional[Dict[str, Any]] = None,
    competition: Optional[Dict[str, Any]] = None,
    schedule: Optional[Dict[str, Any]] = None,
    forecast: Optional[Dict[str, Any]] = None,
    weights: Optional[Dict[str, float]] = None,
    data_freshness_days: Optional[int] = None,
) -> ScoreGraphResult:
    """Build the course opportunity graph for one course type.

    All evidence arguments are optional, flexible dicts. Missing ones become
    ``missing`` nodes that contribute nothing; the remaining node weights
    renormalize to sum to 1 before scoring.
    """
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    sources = {
        "historical_performance": historical,
        "public_demand": demand,
        "course_relative_performance": course_relative,
        "competition_gap": competition,
        "schedule_strength": schedule,
        "forecast_expected_students": forecast,
    }

    # 1. Build raw (pre-renormalization) node data.
    raw: List[Dict[str, Any]] = []
    for key, extractor in _EXTRACTORS.items():
        subscore, value, conf, reasons = extractor(sources[key])
        missing = subscore is None
        raw.append({
            "key": key,
            "subscore": subscore,
            "value": value,
            "confidence": 0.0 if missing else conf,
            "reasons": reasons,
            "missing": missing,
            "base_weight": weights.get(key, 0.0),
        })

    # 2. Renormalize weights across present nodes.
    present_weight = sum(r["base_weight"] for r in raw if not r["missing"])
    nodes: List[ScoreNode] = []
    weighted_sum = 0.0
    conf_weighted = 0.0
    for r in raw:
        if r["missing"] or present_weight <= 0:
            weight = 0.0
            contribution = 0.0
        else:
            weight = r["base_weight"] / present_weight
            contribution = round(weight * r["subscore"], 2)
            weighted_sum += contribution
            conf_weighted += weight * r["confidence"]
        nodes.append(ScoreNode(
            key=r["key"],
            label=_NODE_LABELS.get(r["key"], r["key"]),
            value=r["value"],
            weight=round(weight, 4),
            confidence=round(r["confidence"], 2),
            contribution=contribution,
            reasons=r["reasons"],
            missing=r["missing"],
        ))

    # 3. Confidence penalty from the available history/fields.
    hist = historical or {}
    has_history = bool(hist) and bool(_num(hist.get("total_classes")))
    missing_fields: List[str] = []
    if hist.get("fill_rate_percent") in (None, ""):
        missing_fields.append("fill_rate")
    if forecast is None:
        missing_fields.append("forecast")
    penalty = compute_confidence_penalty(
        sample_size=int(_num(hist.get("total_classes")) or 0) or None,
        historical_confidence=hist.get("confidence"),
        forecast_confidence=(forecast or {}).get("confidence"),
        data_freshness_days=data_freshness_days,
        missing_fields=missing_fields,
        has_history=has_history,
    )

    final_score = round(_clamp(weighted_sum - penalty.penalty_points))
    confidence = round(conf_weighted, 2)

    # 4. Recommendation + headline reasons.
    top = sorted(
        (n for n in nodes if not n.missing),
        key=lambda n: n.contribution, reverse=True,
    )
    reasons: List[str] = []
    if top:
        reasons.append(
            f"Largest driver: {top[0].label} (+{top[0].contribution:.0f})."
        )
    if penalty.penalty_points > 0:
        reasons.append(
            f"Confidence penalty −{penalty.penalty_points:.0f} "
            f"({penalty.confidence_level})."
        )
    recommendation = recommend_course(
        final_score, course_label=label or course_type,
    )

    return ScoreGraphResult(
        course_type=course_type,
        label=label or course_type,
        final_score=final_score,
        recommendation=recommendation,
        nodes=nodes,
        penalty=penalty,
        reasons=reasons,
        confidence=confidence,
    )
