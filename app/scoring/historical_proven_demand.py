"""
Proven ALLCPR demand scoring from real historical ZIP outcomes.

This is the truth-set counterpart to the national modeled score. It only uses
real Enrollware-derived ZIP rows where they exist; it never fabricates student
performance for ZIPs without history and never blends nearby history into a
national ZIP unless a future layer labels that explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


COURSE_LABELS = {
    "arc_cpr_students": "ARC CPR",
    "arc_bls_students": "ARC BLS",
    "aha_bls_students": "AHA BLS",
}
CONFIDENCE_FACTORS = {"low": 0.65, "medium": 0.85, "high": 1.0}


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _norm(value: Any, high: float) -> Optional[float]:
    val = _num(value)
    if val is None or high <= 0:
        return None
    return _clamp(100.0 * val / high)


def _class_count_score(class_count: Any) -> Optional[float]:
    count = _num(class_count)
    if count is None:
        return None
    if count <= 0:
        return 0.0
    if count < 5:
        return _clamp(40.0 * count / 5.0)
    if count < 20:
        return 40.0 + (count - 5.0) * (35.0 / 15.0)
    if count < 50:
        return 75.0 + (count - 20.0) * (25.0 / 30.0)
    return 100.0


def _trend_score(row: Dict[str, Any]) -> Optional[float]:
    raw = str(row.get("trend") or row.get("historical_trend") or "").strip().lower()
    if not raw:
        return None
    if "strong" in raw and ("grow" in raw or "up" in raw):
        return 100.0
    if "grow" in raw or "up" in raw or "increas" in raw:
        return 80.0
    if "declin" in raw or "down" in raw or "decreas" in raw:
        return 30.0
    if "flat" in raw or "stable" in raw or "unknown" in raw:
        return 50.0
    return None


def _total_students(row: Dict[str, Any]) -> Optional[float]:
    total = _num(row.get("total_students"))
    if total is not None:
        return total
    class_count = _num(row.get("class_count") or row.get("classes"))
    avg_students = _num(row.get("avg_students") or row.get("average_students_per_class"))
    if class_count is None or avg_students is None:
        return None
    return class_count * avg_students


def classify_historical_confidence(row: Dict[str, Any]) -> str:
    """Return low/medium/high from the amount of real class history."""
    class_count = _num(row.get("class_count") or row.get("classes"), 0.0) or 0.0
    if class_count < 5:
        return "low"
    if class_count < 20:
        return "medium"
    return "high"


def compute_course_proven_scores(row: Dict[str, Any]) -> Dict[str, Any]:
    """Course-specific proven scores and mix from real student counts."""
    confidence = classify_historical_confidence(row)
    factor = CONFIDENCE_FACTORS[confidence]
    counts = {
        label: _num(row.get(field), 0.0) or 0.0
        for field, label in COURSE_LABELS.items()
    }
    total = sum(counts.values())
    mix = {
        label: round(count / total, 3) if total > 0 else 0.0
        for label, count in counts.items()
    }
    scores = {
        "proven_arc_cpr_score": round((_norm(row.get("arc_cpr_students"), 150) or 0.0) * factor, 1),
        "proven_arc_bls_score": round((_norm(row.get("arc_bls_students"), 150) or 0.0) * factor, 1),
        "proven_aha_bls_score": round((_norm(row.get("aha_bls_students"), 150) or 0.0) * factor, 1),
    }
    best = max(counts.items(), key=lambda item: item[1])[0] if total > 0 else None
    return {
        **scores,
        "best_historical_course": best,
        "historical_course_mix": mix,
    }


def compute_proven_demand_score(row: Dict[str, Any]) -> Dict[str, Any]:
    """Balanced 0-100 proven demand score from real ALLCPR outcomes."""
    components = {
        "avg_students_per_class": (0.30, _norm(row.get("avg_students") or row.get("average_students_per_class"), 12)),
        "students": (0.25, _norm(row.get("recent_students") or _total_students(row), 300)),
        "fill_rate": (0.20, _norm(row.get("fill_rate"), 80)),
        "class_count_confidence": (0.15, _class_count_score(row.get("class_count") or row.get("classes"))),
        "trend": (0.10, _trend_score(row)),
    }
    available = {k: (w, v) for k, (w, v) in components.items() if v is not None}
    if not available:
        score = None
    else:
        weight_sum = sum(w for w, _ in available.values())
        score = round(sum((w / weight_sum) * (v or 0.0)
                          for w, v in available.values()), 1)
    confidence = classify_historical_confidence(row)
    total = _total_students(row)
    return {
        "proven_demand_score": score,
        "historical_confidence": confidence,
        "proven_total_students": round(total, 1) if total is not None else None,
        "proven_score_components": {
            key: round(value, 1) if value is not None else None
            for key, (_, value) in components.items()
        },
        **compute_course_proven_scores(row),
    }


def explain_proven_demand(row: Dict[str, Any]) -> str:
    """Human explanation for the proven demand score."""
    scored = compute_proven_demand_score(row)
    score = scored.get("proven_demand_score")
    confidence = scored.get("historical_confidence")
    if score is None:
        return "No usable ALLCPR class/student history for a proven demand score."
    pieces = [f"Real ALLCPR history scores {score:.1f}/100 with {confidence} confidence"]
    total = scored.get("proven_total_students")
    if total is not None:
        pieces.append(f"{total:g} students")
    classes = _num(row.get("class_count") or row.get("classes"))
    if classes is not None:
        pieces.append(f"{classes:g} classes")
    avg = _num(row.get("avg_students") or row.get("average_students_per_class"))
    if avg is not None:
        pieces.append(f"{avg:g} avg students/class")
    return ", ".join(pieces) + "."

