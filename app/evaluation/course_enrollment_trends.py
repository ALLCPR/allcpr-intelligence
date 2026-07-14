"""Historical enrollment trend by course type (Enrollware only).

For each target course type we fit a simple least-squares regression of
*monthly average enrollment* over time. The point of this section is direction:
is a course's enrollment improving, declining, or flat across history? It is a
historical-direction signal, never a guaranteed forecast.

Enrollware history only — no Google, Census, Yelp, Foursquare, Adzuna or other
public-API signal feeds this. Pure, deterministic math (the least-squares and
Pearson helpers are reused from the scoring package so the regression maths
lives in one place). Nothing here changes scoring.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.collectors.enrollware import (
    COURSE_TYPE_LABELS,
    EnrollwareClassRecord,
    held_class_cutoff_month,
    is_held_class,
)
from app.scoring.backtest import pearson
from app.scoring.regression_validation import simple_linear_regression

TARGET_COURSE_TYPES = ("arc_cpr", "arc_bls", "aha_bls")

# Need at least this many time points before a line is fit at all.
MIN_TREND_POINTS = 3
# |slope| (students/class per month) at or below this reads as flat, not a trend.
FLAT_SLOPE_EPS = 0.01

BASIS = "Enrollware historical enrollment only"

CSV_COLUMNS = (
    "course_type",
    "period",
    "average_enrollment",
    "class_count",
    "trend_direction",
    "slope",
    "r_squared",
    "pearson",
    "confidence_label",
)


def _usable(records: Iterable[EnrollwareClassRecord],
            cutoff_month: Optional[str] = None) -> List[EnrollwareClassRecord]:
    # Held classes only — the same completed-month, real-attendance basis used
    # by the benchmark table and scoring (see app.collectors.enrollware).
    cutoff = cutoff_month or held_class_cutoff_month()
    return [
        r for r in records
        if is_held_class(r, cutoff)
        and r.course_type != "unknown_course_type"
    ]


def _monthly_points(records: List[EnrollwareClassRecord]) -> List[Dict[str, Any]]:
    """Monthly average enrollment, one point per calendar month, sorted ascending."""
    buckets: Dict[str, List[int]] = {}
    for r in records:
        if r.month and r.enrolled is not None:
            buckets.setdefault(r.month, []).append(int(r.enrolled))
    points: List[Dict[str, Any]] = []
    for period in sorted(buckets):
        vals = buckets[period]
        points.append({
            "period": period,
            "average_enrollment": round(sum(vals) / len(vals), 2),
            "class_count": len(vals),
        })
    return points


def _class_points(records: List[EnrollwareClassRecord]) -> List[Dict[str, Any]]:
    """Fallback: one point per individual dated class (when months are too few)."""
    dated = [r for r in records if r.date and r.enrolled is not None]
    points: List[Dict[str, Any]] = []
    for r in sorted(dated, key=lambda r: r.date):
        points.append({
            "period": r.date,
            "average_enrollment": float(int(r.enrolled)),
            "class_count": 1,
        })
    return points


def _month_x(period: str, base: Tuple[int, int]) -> float:
    """Calendar-aware month index from the first period (respects gaps)."""
    y, m = (int(v) for v in period.split("-")[:2])
    by, bm = base
    return float((y - by) * 12 + (m - bm))


def _day_x(period: str, base: str) -> float:
    """Day index from the first date (used by the per-class fallback)."""
    from datetime import date
    y, m, d = (int(v) for v in period.split("-")[:3])
    by, bm, bd = (int(v) for v in base.split("-")[:3])
    return float((date(y, m, d) - date(by, bm, bd)).days)


def _confidence_label(n: int) -> str:
    if n < MIN_TREND_POINTS:
        return "Insufficient"
    if n < 6:
        return "Low"
    if n < 12:
        return "Medium"
    return "High"


def _trend_direction(slope: Optional[float], n: int) -> str:
    if n < MIN_TREND_POINTS or slope is None:
        return "insufficient data"
    if slope > FLAT_SLOPE_EPS:
        return "improving"
    if slope < -FLAT_SLOPE_EPS:
        return "declining"
    return "flat"


def _business_note(label: str, direction: str, n: int, total_classes: int) -> str:
    if direction == "insufficient data":
        return (
            f"{label} has too little dated history ({n} time point(s)) to fit a "
            "reliable trend. Treat direction as unknown until more classes run."
        )
    sample = "a large" if total_classes >= 200 else (
        "a moderate" if total_classes >= 50 else "a small")
    phrasing = {
        "improving": "enrollment has been trending up over time",
        "declining": "enrollment has been trending down over time",
        "flat": "enrollment has held roughly flat over time",
    }[direction]
    return (
        f"{label} has {sample} historical sample ({total_classes} classes); "
        f"{phrasing}. Use this as a historical direction signal, not a "
        "guaranteed future prediction."
    )


def _build_one(course_type: str,
               recs: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    label = COURSE_TYPE_LABELS.get(course_type, course_type)
    all_enrolled = [int(r.enrolled) for r in recs if r.enrolled is not None]
    total_classes = len(all_enrolled)
    avg = round(sum(all_enrolled) / total_classes, 2) if total_classes else None

    points = _monthly_points(recs)
    basis = "monthly_average_enrollment"
    if len(points) < MIN_TREND_POINTS:
        # Too few months — fall back to individual class records.
        fallback = _class_points(recs)
        if len(fallback) > len(points):
            points, basis = fallback, "individual_class_records"

    n = len(points)
    slope = intercept = r_squared = pear = None
    if n >= MIN_TREND_POINTS:
        if basis == "monthly_average_enrollment":
            base = tuple(int(v) for v in points[0]["period"].split("-")[:2])
            xs = [_month_x(p["period"], base) for p in points]
        else:
            base = points[0]["period"]
            xs = [_day_x(p["period"], base) for p in points]
        ys = [float(p["average_enrollment"]) for p in points]
        fit = simple_linear_regression(xs, ys)
        if fit is not None:
            slope, intercept, r_squared = fit
            slope = round(slope, 4)
            intercept = round(intercept, 4)
            r_squared = round(r_squared, 4) if r_squared is not None else None
            pr = pearson(xs, ys)
            pear = round(pr, 4) if pr is not None else None

    direction = _trend_direction(slope, n)
    return {
        "course_type": label,
        "course_type_key": course_type,
        "basis": basis,
        "n": n,
        "total_classes": total_classes,
        "average_students_per_class": avg,
        "slope": slope,
        "intercept": intercept,
        "r_squared": r_squared,
        "pearson": pear,
        "trend_direction": direction,
        "confidence_label": _confidence_label(n),
        "business_note": _business_note(label, direction, n, total_classes),
        "points": points,
    }


def build_course_enrollment_trends(
    records: List[EnrollwareClassRecord],
    course_types: Iterable[str] = TARGET_COURSE_TYPES,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    cutoff = held_class_cutoff_month(today)
    usable = _usable(records, cutoff)
    trends = [
        _build_one(ct, [r for r in usable if r.course_type == ct])
        for ct in course_types
    ]
    return {
        "basis": BASIS,
        "source": "Enrollware historical classes",
        "x_label": "Month",
        "y_label": "Average students per class",
        "cutoff_month": cutoff,
        "note": (
            "Completed months only. Future and current-partial months are "
            "excluded so not-yet-held classes do not fake a decline."
        ),
        "trends": trends,
    }


def write_trends_json(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_trends_csv(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for trend in payload.get("trends") or []:
            for p in trend.get("points") or []:
                writer.writerow({
                    "course_type": trend["course_type"],
                    "period": p["period"],
                    "average_enrollment": p["average_enrollment"],
                    "class_count": p["class_count"],
                    "trend_direction": trend["trend_direction"],
                    "slope": trend["slope"],
                    "r_squared": trend["r_squared"],
                    "pearson": trend["pearson"],
                    "confidence_label": trend["confidence_label"],
                })
