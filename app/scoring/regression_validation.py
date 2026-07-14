"""Score vs Actual Enrollment Validation.

An *honest sanity-check*, NOT a future-prediction model. We fit a simple
linear regression of actual historical enrollment (y) on the generated
opportunity score (x) to see whether higher scores have *historically* lined
up with higher enrollment.

Future enrollment depends on ads, price, schedule timing, student behaviour,
competition changes, seasonality, instructor availability and Red Cross
visibility — none of which are known here. So this module only ever validates
score *alignment* against known history; it never claims a guaranteed
prediction, and it never fabricates an enrollment value.

Pure math, no network — fully unit-testable. Pearson + Spearman are reused
from :mod:`app.scoring.backtest` so the correlation maths stays in one place.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.scoring.backtest import pearson, spearman

X_LABEL = "Opportunity score"
Y_LABEL = "Actual historical enrollment"

# Drawn only when at least this many usable points exist.
MIN_POINTS = 3

NOT_ENOUGH_WARNING = "Not enough historical outcome data for reliable regression."
HONESTY_NOTE = (
    "This validates score alignment against known historical enrollment only. "
    "Future results may change due to ads, price, schedule timing, student "
    "behavior, and competition."
)


def _num(value: Any) -> Optional[float]:
    """Coerce to float only for real numbers (bools are *not* numbers here)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _point_label(city: Optional[str], location: Optional[str], label: str) -> str:
    """Human-readable identity for one validation dot."""
    parts = [str(p) for p in (city, label, location) if p]
    return " — ".join(parts)


def simple_linear_regression(
    xs: List[float], ys: List[float]
) -> Optional[Tuple[float, float, Optional[float]]]:
    """Ordinary least-squares fit ``y = m*x + b``.

    Returns ``(slope, intercept, r_squared)`` or ``None`` when a line can't be
    fit (n < 2 or no spread in x). ``r_squared`` is ``None`` when y has no
    spread (an undefined coefficient of determination).
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None  # vertical: every score identical, slope undefined
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    return slope, intercept, r_squared


def _course_points(perf: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One point per usable course candidate.

    ``x`` is the generated opportunity score — preferring the independent
    Course Opportunity Graph ``final_score`` when present, otherwise the
    course-performance score. ``y`` is the *actual* historical enrollment —
    the average students/class when available, otherwise the total students
    (the standard enrollment outcome already carried per course type).

    Rows missing either the score or the enrollment are skipped; nothing is
    invented.
    """
    course_types = perf.get("course_types") or []
    graph = perf.get("evaluation_graph") or {}
    by_type: Dict[Any, Dict[str, Any]] = {}
    by_label: Dict[Any, Dict[str, Any]] = {}
    for c in graph.get("course_opportunity_graph") or []:
        if c.get("course_type") is not None:
            by_type[c.get("course_type")] = c
        if c.get("label") is not None:
            by_label[c.get("label")] = c

    city = perf.get("area_label")
    location = perf.get("location") or perf.get("location_name") or city
    points: List[Dict[str, Any]] = []
    for ct in course_types:
        course_type = ct.get("course_type")
        label = ct.get("label") or course_type or "course"

        # Opportunity score: independent graph score first, perf score fallback.
        graph_course = by_type.get(course_type) or by_label.get(ct.get("label"))
        score = _num((graph_course or {}).get("final_score")) if graph_course else None
        if score is None:
            score = _num(ct.get("course_performance_score"))

        # Actual enrollment: average preferred, total as the documented fallback.
        enrollment = _num(ct.get("average_students_per_class"))
        enrollment_basis = "average_students_per_class"
        if enrollment is None:
            enrollment = _num(ct.get("total_students"))
            enrollment_basis = "total_students"

        if score is None or enrollment is None:
            continue  # never fabricate a missing score or enrollment

        points.append({
            "label": _point_label(city, location, str(label)),
            "city": city,
            "location": location,
            "course_type": course_type or label,
            "score": round(score, 2),
            "actual_enrollment": round(enrollment, 2),
            "historical_class_count": ct.get("total_classes")
            or ct.get("classes_with_enrollment"),
            "enrollment_basis": enrollment_basis,
            "course_label": label,
        })
    return points


def build_regression_validation(
    perf: Optional[Dict[str, Any]],
    points: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the ``regression_validation`` payload for a course-performance area.

    Pass ``points`` directly to bypass extraction (used by tests); otherwise
    they are derived from ``perf['course_types']`` (+ the optional opportunity
    graph). The result is JSON-serialisable and consumed by every report
    renderer. The regression line is only fit when ``n >= MIN_POINTS`` and the
    scores actually vary.
    """
    if points is None:
        points = _course_points(perf or {})
    n = len(points)
    result: Dict[str, Any] = {
        "n": n,
        "slope": None,
        "intercept": None,
        "r_squared": None,
        "pearson": None,
        "spearman": None,
        "enough_data": False,
        "warning": None,
        "x_label": X_LABEL,
        "y_label": Y_LABEL,
        # Honesty fields — this is a sanity check, not a forecast.
        "validation_only": True,
        "note": HONESTY_NOTE,
        "points": points,
    }

    if n < MIN_POINTS:
        result["warning"] = NOT_ENOUGH_WARNING
        return result

    xs = [p["score"] for p in points]
    ys = [p["actual_enrollment"] for p in points]
    fit = simple_linear_regression(xs, ys)
    if fit is None:
        # Enough points but no spread in score → no meaningful line.
        result["warning"] = (
            "Opportunity scores do not vary across the usable history, so no "
            "regression line can be fit."
        )
        return result

    slope, intercept, r_squared = fit
    result["enough_data"] = True
    result["slope"] = round(slope, 4)
    result["intercept"] = round(intercept, 4)
    result["r_squared"] = round(r_squared, 4) if r_squared is not None else None
    pr = pearson(xs, ys)
    sp = spearman(xs, ys)
    result["pearson"] = round(pr, 4) if pr is not None else None
    result["spearman"] = round(sp, 4) if sp is not None else None
    return result
