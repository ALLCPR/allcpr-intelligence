"""
Course-performance aggregation (Phase 4B).

Takes normalized Enrollware class records (see
``app.collectors.enrollware``) and rolls them up into per-course-type
performance metrics for one area (city) and overall. Pure and deterministic:
same records in, same metrics out. Every metric that depends on a field the
export did not contain is returned as ``None`` ("unknown") — never imputed.

Metrics per course type:
  - total_classes
  - total_students / classes_with_enrollment
  - average_students_per_class, median_students_per_class
  - fill_rate_percent          (only when capacity exists)
  - cancelled_classes, cancellation_rate_percent
  - revenue_estimate           (only when price exists; labeled estimate)
  - weekday_vs_weekend         (classes + avg students per day-part)
  - by_city                    (per-location performance)
  - trend_by_month             (avg students per YYYY-MM, when dates exist)
"""
from __future__ import annotations

import re
from dataclasses import replace
from statistics import median
from typing import Any, Dict, List, Optional

from app.collectors.enrollware import COURSE_TYPE_LABELS, EnrollwareClassRecord
from app.enrichers.course_classifier import _detect_provider, _norm


DECISION_COURSE_TYPES = ("aha_bls", "arc_bls", "arc_cpr")

_BLS_RE = re.compile(r"\bbls\b")
_ARC_CPR_RE = re.compile(r"\bcpr\b|first\s*aid|\baed\b")


def _avg(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _filter_records(
    records: List[EnrollwareClassRecord],
    city: Optional[str],
) -> List[EnrollwareClassRecord]:
    """Keep only records for ``city`` (case-insensitive substring), or all."""
    if not city:
        return list(records)
    needles = [city.strip().lower()]
    if "," in city:
        for part in reversed([p.strip().lower() for p in city.split(",") if p.strip()]):
            if part not in needles:
                needles.append(part)
    matched = []
    for needle in needles:
        matched = [
            r for r in records
            if (r.city and needle in r.city.lower())
            or (r.location and needle in r.location.lower())
        ]
        if matched:
            break
    # If the export carries no city/location columns we cannot filter; fall
    # back to the full set rather than silently dropping everything.
    return matched if matched else list(records)


def _rollup_decision_course_type(r: EnrollwareClassRecord) -> Optional[str]:
    """Map Enrollware classes into the three center-decision course buckets.

    The raw classifier keeps ALLCPR, skills sessions, and provider-less hybrid
    CPR/First Aid honest as separate buckets. For center-opening decisions we
    only compare the three courses the business is deciding between. Records
    are folded in only when the class name itself proves the provider/course;
    otherwise they are excluded instead of guessed into AHA/ARC demand.
    """
    course_type = _norm(r.course_type)
    if course_type in DECISION_COURSE_TYPES:
        return course_type

    name = _norm(r.class_name)
    provider = _detect_provider(name)
    has_bls = bool(_BLS_RE.search(name)) or "basic life support" in name
    has_arc_cpr = bool(_ARC_CPR_RE.search(name))

    if provider == "aha" and has_bls:
        return "aha_bls"
    if provider == "arc" and has_bls:
        return "arc_bls"
    if provider == "arc" and has_arc_cpr:
        return "arc_cpr"
    return None


def _rollup_decision_records(
    records: List[EnrollwareClassRecord],
) -> tuple[List[EnrollwareClassRecord], Dict[str, Any]]:
    """Return records safely reassigned to decision buckets plus rollup stats."""
    rolled: List[EnrollwareClassRecord] = []
    merged: Dict[str, int] = {}
    dropped: Dict[str, int] = {}

    for r in records:
        target = _rollup_decision_course_type(r)
        if target is None:
            dropped[r.course_type] = dropped.get(r.course_type, 0) + 1
            continue
        if target != r.course_type:
            merged[f"{r.course_type}->{target}"] = (
                merged.get(f"{r.course_type}->{target}", 0) + 1
            )
        rolled.append(replace(
            r,
            course_type=target,
            course_type_label=COURSE_TYPE_LABELS.get(target, target),
        ))

    return rolled, {
        "basis": (
            "Center-opening decisions use only AHA BLS, ARC BLS, and ARC CPR. "
            "ALLCPR, skills-session, and hybrid records are merged only when "
            "the Enrollware class name proves the matching provider/course; "
            "otherwise they are excluded from those course scores."
        ),
        "input_records": len(records),
        "included_records": len(rolled),
        "dropped_records": len(records) - len(rolled),
        "merged_by_source_target": merged,
        "dropped_by_source_course_type": dropped,
    }


def rollup_decision_records(
    records: List[EnrollwareClassRecord],
) -> tuple[List[EnrollwareClassRecord], Dict[str, Any]]:
    """Public wrapper for report sections that need the same course rollup."""
    return _rollup_decision_records(records)


def _day_part_breakdown(records: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for part in ("weekday", "weekend"):
        subset = [r for r in records if r.day_part == part]
        enrolled = [r.enrolled for r in subset if r.enrolled is not None]
        out[part] = {
            "classes": len(subset),
            "total_students": sum(enrolled) if enrolled else None,
            "average_students_per_class": _avg([float(e) for e in enrolled]),
        }
    return out


def _by_city(records: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    grouped: Dict[str, List[EnrollwareClassRecord]] = {}
    for r in records:
        key = r.city or r.location
        if not key:
            continue
        grouped.setdefault(key, []).append(r)
    out: Dict[str, Any] = {}
    for key, subset in grouped.items():
        enrolled = [r.enrolled for r in subset if r.enrolled is not None]
        out[key] = {
            "classes": len(subset),
            "average_students_per_class": _avg([float(e) for e in enrolled]),
        }
    return out


def _trend_by_month(records: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    grouped: Dict[str, List[EnrollwareClassRecord]] = {}
    for r in records:
        if not r.month:
            continue
        grouped.setdefault(r.month, []).append(r)
    out: Dict[str, Any] = {}
    for month in sorted(grouped):
        subset = grouped[month]
        enrolled = [r.enrolled for r in subset if r.enrolled is not None]
        out[month] = {
            "classes": len(subset),
            "average_students_per_class": _avg([float(e) for e in enrolled]),
        }
    return out


def _course_type_metrics(
    course_type: str,
    records: List[EnrollwareClassRecord],
    modeled_price: Optional[float] = None,
) -> Dict[str, Any]:
    enrolled = [r.enrolled for r in records if r.enrolled is not None]
    enrolled_f = [float(e) for e in enrolled]
    held = [e for e in enrolled if e > 0]  # classes that actually ran
    capacities = [(r.enrolled, r.capacity) for r in records
                  if r.enrolled is not None and r.capacity is not None
                  and r.capacity > 0]
    prices = [(r.enrolled, r.price) for r in records
              if r.enrolled is not None and r.price is not None]
    cancelled = [r for r in records if r.cancelled is True]
    cancel_known = [r for r in records if r.cancelled is not None]

    fill_rate: Optional[float] = None
    if capacities:
        seats_filled = sum(e for e, _ in capacities)
        seats_total = sum(c for _, c in capacities)
        if seats_total > 0:
            fill_rate = round(100.0 * seats_filled / seats_total, 1)

    # Revenue: prefer real prices from the export; otherwise fall back to a
    # clearly-labeled modeled price (ALLCPR median) so the figure is never
    # presented as measured truth.
    revenue_estimate: Optional[float] = None
    revenue_basis: Optional[str] = None
    if prices:
        revenue_estimate = round(sum(e * p for e, p in prices), 2)
        revenue_basis = "export_price"
    elif modeled_price and enrolled:
        revenue_estimate = round(sum(enrolled) * modeled_price, 2)
        revenue_basis = "modeled_allcpr_median"

    cancellation_rate: Optional[float] = None
    if cancel_known:
        cancellation_rate = round(100.0 * len(cancelled) / len(cancel_known), 1)

    return {
        "course_type": course_type,
        "label": COURSE_TYPE_LABELS.get(course_type, course_type),
        "total_classes": len(records),
        "classes_with_enrollment": len(enrolled),
        "classes_held": len(held),                  # enrolled > 0
        "total_students": sum(enrolled) if enrolled else None,
        "average_students_per_class": _avg(enrolled_f),
        "average_students_per_held_class": _avg([float(e) for e in held]),
        "median_students_per_class": (
            round(float(median(enrolled_f)), 2) if enrolled_f else None
        ),
        "fill_rate_percent": fill_rate,
        "cancelled_classes": len(cancelled) if cancel_known else None,
        "cancellation_rate_percent": cancellation_rate,
        "revenue_estimate": revenue_estimate,
        "revenue_basis": revenue_basis,
        "weekday_vs_weekend": _day_part_breakdown(records),
        "by_city": _by_city(records),
        "trend_by_month": _trend_by_month(records),
        # Honest coverage flags so the report can label unknowns.
        "enrollment_known": bool(enrolled),
        "capacity_known": bool(capacities),
        "price_known": bool(prices),
    }


def build_course_performance(
    records: List[EnrollwareClassRecord],
    city: Optional[str] = None,
    state: Optional[str] = None,
    modeled_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate Enrollware records into per-course-type performance.

    Returns ``None`` when there are no records (so callers can omit the
    section entirely). ``city`` narrows to one area; when the export lacks a
    city/location column the full dataset is used and ``area_is_filtered`` is
    False so the report can say "ALLCPR-wide, not area-specific".
    """
    if not records:
        return None

    filtered = _filter_records(records, city)
    area_is_filtered = bool(city) and len(filtered) != len(records)
    decision_records, course_rollup = _rollup_decision_records(filtered)

    grouped: Dict[str, List[EnrollwareClassRecord]] = {}
    for r in decision_records:
        grouped.setdefault(r.course_type, []).append(r)

    course_types = [
        _course_type_metrics(ct, recs, modeled_price=modeled_price)
        for ct, recs in grouped.items()
    ]
    # Rank by average enrollment (unknowns last), then by class volume.
    course_types.sort(
        key=lambda c: (
            c["average_students_per_class"] is not None,
            c["average_students_per_class"] or 0.0,
            c["total_classes"],
        ),
        reverse=True,
    )

    # Overall aggregate across every decision-eligible course type in the
    # filtered set. Excluded records stay reported in ``course_rollup``.
    all_enrolled = [
        float(r.enrolled) for r in decision_records if r.enrolled is not None
    ]
    overall = {
        "total_classes": len(decision_records),
        "total_students": int(sum(all_enrolled)) if all_enrolled else None,
        "average_students_per_class": _avg(all_enrolled),
        "median_students_per_class": (
            round(float(median(all_enrolled)), 2) if all_enrolled else None
        ),
    }

    coverage = {
        "enrollment": any(c["enrollment_known"] for c in course_types),
        "capacity": any(c["capacity_known"] for c in course_types),
        "price": any(c["price_known"] for c in course_types),
        "dates": any(r.month for r in decision_records),
    }

    area_label = ", ".join(p for p in (city, state) if p) or "ALLCPR-wide"

    return {
        "area_label": area_label,
        "area_is_filtered": area_is_filtered,
        "total_classes": len(decision_records),
        "course_types": course_types,
        "overall": overall,
        "data_coverage": coverage,
        "modeled_price": modeled_price,
        "course_rollup": course_rollup,
    }
