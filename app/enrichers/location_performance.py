"""
Location performance enricher (STEP 5).

A location-centric lens on ALLCPR's Enrollware history. Where
``course_performance`` rolls classes up *by course type*, this rolls them up *by
place* (or city, or course type) and answers operational questions:

  - utilization   — how full do this location's classes run (seats filled /
    seats offered, where capacity is known)?
  - growth        — is enrollment trending up or down over time?
  - average enrollment / total students / classes
  - top / weakest courses at the location
  - revenue       — from export price, else a clearly-labeled modeled figure

Pure and deterministic. Any metric that depends on a field the export did not
contain stays ``None`` — never imputed. Returns ``None`` when there are no
records so callers omit the section.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional

from app.collectors.enrollware import COURSE_TYPE_LABELS, EnrollwareClassRecord

# How a record is bucketed for each supported grouping.
_GROUPERS = {
    "city": lambda r: r.city or r.location,
    "location": lambda r: r.location or r.city,
    "course_type": lambda r: r.course_type,
}

VALID_GROUP_BY = tuple(_GROUPERS)


def _avg(values: List[float]) -> Optional[float]:
    return round(mean(values), 2) if values else None


def _utilization(records: List[EnrollwareClassRecord]) -> Optional[float]:
    pairs = [
        (r.enrolled, r.capacity) for r in records
        if r.enrolled is not None and r.capacity is not None and r.capacity > 0
    ]
    if not pairs:
        return None
    filled = sum(e for e, _ in pairs)
    offered = sum(c for _, c in pairs)
    return round(100.0 * filled / offered, 1) if offered > 0 else None


def _growth_ratio(records: List[EnrollwareClassRecord]) -> Optional[float]:
    """Later-half vs earlier-half monthly average enrollment. None if <2 months."""
    by_month: Dict[str, List[int]] = {}
    for r in records:
        if r.month and r.enrolled is not None:
            by_month.setdefault(r.month, []).append(r.enrolled)
    months = sorted(by_month)
    if len(months) < 2:
        return None
    monthly_avg = [mean(by_month[m]) for m in months]
    half = len(monthly_avg) // 2
    earlier = mean(monthly_avg[:half]) if half else monthly_avg[0]
    later = mean(monthly_avg[half:])
    return round(later / earlier, 2) if earlier > 0 else None


def _revenue(
    records: List[EnrollwareClassRecord], modeled_price: Optional[float]
) -> tuple[Optional[float], Optional[str]]:
    priced = [(r.enrolled, r.price) for r in records
              if r.enrolled is not None and r.price is not None]
    if priced:
        return round(sum(e * p for e, p in priced), 2), "export_price"
    enrolled = [r.enrolled for r in records if r.enrolled is not None]
    if modeled_price and enrolled:
        return round(sum(enrolled) * modeled_price, 2), "modeled_allcpr_median"
    return None, None


def _course_breakdown(
    records: List[EnrollwareClassRecord],
) -> List[Dict[str, Any]]:
    """Per-course-type average enrollment within one group, best first."""
    by_ct: Dict[str, List[int]] = {}
    for r in records:
        if r.enrolled is not None:
            by_ct.setdefault(r.course_type, []).append(r.enrolled)
    out = [
        {
            "course_type": ct,
            "label": COURSE_TYPE_LABELS.get(ct, ct),
            "classes": len(vals),
            "average_students_per_class": _avg([float(v) for v in vals]),
        }
        for ct, vals in by_ct.items()
    ]
    out.sort(key=lambda c: c["average_students_per_class"] or 0.0, reverse=True)
    return out


def _group_metrics(
    key: str,
    records: List[EnrollwareClassRecord],
    modeled_price: Optional[float],
) -> Dict[str, Any]:
    enrolled = [r.enrolled for r in records if r.enrolled is not None]
    revenue, revenue_basis = _revenue(records, modeled_price)
    breakdown = _course_breakdown(records)
    # "Real" named course types only for top/weakest (skip the unknown bucket).
    named = [c for c in breakdown
             if c["course_type"] != "unknown_course_type"
             and c["average_students_per_class"] is not None]
    return {
        "key": key,
        "classes": len(records),
        "total_students": sum(enrolled) if enrolled else None,
        "average_students_per_class": _avg([float(e) for e in enrolled]),
        "utilization_percent": _utilization(records),
        "growth_ratio": _growth_ratio(records),
        "revenue_estimate": revenue,
        "revenue_basis": revenue_basis,
        "top_courses": named[:3],
        "weakest_courses": list(reversed(named[-3:])) if len(named) > 3 else [],
        "course_breakdown": breakdown,
    }


def build_location_performance(
    records: List[EnrollwareClassRecord],
    group_by: str = "city",
    modeled_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate Enrollware records by place / city / course type.

    ``group_by`` is one of ``VALID_GROUP_BY``. Records whose grouping key is
    unknown are collected under a single ``"Unknown"`` bucket rather than
    dropped. Groups are ranked by average enrollment (unknowns last), then by
    class volume. Returns ``None`` when there are no records.
    """
    if not records:
        return None
    if group_by not in _GROUPERS:
        group_by = "city"
    grouper = _GROUPERS[group_by]

    grouped: Dict[str, List[EnrollwareClassRecord]] = {}
    for r in records:
        raw_key = grouper(r)
        if group_by == "course_type":
            key = COURSE_TYPE_LABELS.get(raw_key, raw_key) if raw_key else "Unknown"
        else:
            key = (str(raw_key).strip() if raw_key else "") or "Unknown"
        grouped.setdefault(key, []).append(r)

    groups = [
        _group_metrics(key, recs, modeled_price)
        for key, recs in grouped.items()
    ]
    groups.sort(
        key=lambda g: (
            g["average_students_per_class"] is not None,
            g["average_students_per_class"] or 0.0,
            g["classes"],
        ),
        reverse=True,
    )

    return {
        "group_by": group_by,
        "total_classes": len(records),
        "group_count": len(groups),
        "groups": groups,
    }
