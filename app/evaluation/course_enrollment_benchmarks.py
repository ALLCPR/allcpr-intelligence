"""Historical enrollment benchmark by course type.

Uses Enrollware history only. No public API signals, no scoring changes.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

from app.collectors.enrollware import (
    COURSE_TYPE_LABELS,
    EnrollwareClassRecord,
    is_held_class,
)

TARGET_COURSE_TYPES = ("arc_cpr", "arc_bls", "aha_bls")
LOW_CONFIDENCE_CLASS_COUNT = 10

CSV_COLUMNS = (
    "course_type",
    "average_students_per_class",
    "allcpr_overall_average",
    "average_fill_rate_pct",
    "allcpr_overall_fill_rate_pct",
    "fill_rate_class_count",
    "total_students",
    "class_count",
    "median_students_per_class",
    "min_students",
    "max_students",
    "comparison_vs_allcpr_average",
    "percent_above_or_below_allcpr_average",
    "difference_vs_allcpr_average",
    "percent_vs_allcpr_average",
    "data_confidence",
    "confidence_label",
    "recommendation_note",
)


def _usable_records(records: Iterable[EnrollwareClassRecord]) -> List[EnrollwareClassRecord]:
    # Held classes only (ran with real attendance in a completed month); the
    # course-type catalog excludes the unclassifiable bucket from benchmarks.
    return [
        r for r in records
        if is_held_class(r)
        and r.course_type != "unknown_course_type"
    ]


def _avg(values: List[int]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _fill_rate_pct(pairs: List[tuple]) -> Optional[float]:
    """Mean per-class fill rate (enrolled / capacity) as a 0–100 percent.

    Only classes that carry a positive capacity contribute — capacity is often
    blank in the export, and we never invent it.
    """
    ratios = [e / c for e, c in pairs if c and c > 0 and e is not None]
    return round(100.0 * sum(ratios) / len(ratios), 1) if ratios else None


def _pct(diff: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if diff is None or not baseline:
        return None
    return round(100.0 * diff / baseline, 1)


def _confidence(class_count: int) -> str:
    return "low" if class_count < LOW_CONFIDENCE_CLASS_COUNT else "normal"


def _confidence_label(class_count: int) -> str:
    """Human-facing confidence for the chart: 'Low' for thin samples, else 'High'."""
    return "Low" if class_count < LOW_CONFIDENCE_CLASS_COUNT else "High"


def _recommendation(label: str, diff: Optional[float], pct: Optional[float],
                    class_count: int) -> str:
    if class_count < LOW_CONFIDENCE_CLASS_COUNT:
        return (
            f"{label} has limited historical sample; treat as low confidence "
            "until more classes run."
        )
    if diff is None or pct is None:
        return f"{label} has no usable enrollment benchmark."
    if diff >= 0.75 or pct >= 12:
        return (
            "Strong historical performer; prioritize if local demand supports it."
        )
    if diff >= -0.25:
        return "Near ALLCPR average; test depending on local healthcare demand."
    return "Below ALLCPR average; test only where local demand is strong."


def _course_row(course_type: str, enrolled: List[int],
                overall_avg: Optional[float],
                cap_pairs: Optional[List[tuple]] = None) -> Dict[str, Any]:
    label = COURSE_TYPE_LABELS.get(course_type, course_type)
    avg = _avg(enrolled)
    diff = round(avg - overall_avg, 2) if avg is not None and overall_avg is not None else None
    pct = _pct(diff, overall_avg)
    cap_pairs = cap_pairs or []
    fill = _fill_rate_pct(cap_pairs)
    fill_n = sum(1 for e, c in cap_pairs if c and c > 0 and e is not None)
    return {
        "course_type": label,
        "course_type_key": course_type,
        "average_students_per_class": avg,
        "allcpr_overall_average": overall_avg,
        "average_fill_rate_pct": fill,
        "fill_rate_class_count": fill_n,
        "total_students": sum(enrolled) if enrolled else None,
        "class_count": len(enrolled),
        "median_students_per_class": (
            round(float(median(enrolled)), 2) if enrolled else None
        ),
        "min_students": min(enrolled) if enrolled else None,
        "max_students": max(enrolled) if enrolled else None,
        "comparison_vs_allcpr_average": diff,
        "percent_above_or_below_allcpr_average": pct,
        # aliases matching the suggested JSON shape
        "difference_vs_allcpr_average": diff,
        "percent_vs_allcpr_average": pct,
        "data_confidence": _confidence(len(enrolled)),
        "confidence_label": _confidence_label(len(enrolled)),
        "recommendation_note": _recommendation(label, diff, pct, len(enrolled)),
    }


def build_course_enrollment_benchmarks(
    records: List[EnrollwareClassRecord],
    course_types: Iterable[str] = TARGET_COURSE_TYPES,
) -> Dict[str, Any]:
    usable = _usable_records(records)
    all_enrolled = [int(r.enrolled) for r in usable if r.enrolled is not None]
    overall_avg = _avg(all_enrolled)
    overall_fill = _fill_rate_pct(
        [(r.enrolled, r.capacity) for r in usable]
    )

    grouped: Dict[str, List[int]] = {ct: [] for ct in course_types}
    grouped_caps: Dict[str, List[tuple]] = {ct: [] for ct in course_types}
    for r in usable:
        if r.course_type in grouped and r.enrolled is not None:
            grouped[r.course_type].append(int(r.enrolled))
            grouped_caps[r.course_type].append((r.enrolled, r.capacity))

    rows = [
        _course_row(ct, grouped[ct], overall_avg, grouped_caps[ct])
        for ct in course_types
    ]
    for r in rows:
        r["allcpr_overall_fill_rate_pct"] = overall_fill
    best = max(
        (r for r in rows if r["average_students_per_class"] is not None),
        key=lambda r: (r["average_students_per_class"], r["class_count"]),
        default=None,
    )
    return {
        "source": "Enrollware historical classes",
        "allcpr_overall_average": overall_avg,
        "allcpr_overall_fill_rate_pct": overall_fill,
        "allcpr_total_students": sum(all_enrolled) if all_enrolled else None,
        "allcpr_class_count": len(all_enrolled),
        "course_benchmarks": rows,
        "strongest_historical_course_type": best.get("course_type") if best else None,
        "strongest_historical_course_type_key": best.get("course_type_key") if best else None,
    }


def write_benchmarks_json(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_benchmarks_csv(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in payload.get("course_benchmarks") or []:
            writer.writerow({k: row.get(k) for k in CSV_COLUMNS})
