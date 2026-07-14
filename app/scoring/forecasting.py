"""
Forecasting layer (STEP 10) — weighted averages, no ML (yet).

Predicts what ALLCPR's *next* class of a given type is likely to do, from its own
history. Deliberately simple and deterministic: a recency-weighted average (more
recent classes count more, via an exponential half-life), never a black box. The
output carries a reserved ``features`` block per course type and a stable
``schema_version`` so a future ML model can be dropped in without changing the
report contract.

Predictions per course type (and overall):
  - expected_students        — recency-weighted mean enrollment
  - expected_fill_rate       — recency-weighted mean of seats filled / offered
  - expected_revenue         — expected_students × price (export, else modeled)

Everything that depends on a missing field stays ``None``. Returns ``None`` when
there is no usable history.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.enrollware import COURSE_TYPE_LABELS, EnrollwareClassRecord

# Bump when the forecast payload shape changes (consumers/ML can gate on it).
SCHEMA_VERSION = 1
METHOD = "recency_weighted_average"

# Half-life for recency weighting, in months: a class this many months older
# than the most recent one counts half as much.
_HALF_LIFE_MONTHS = 6.0


def _month_index(month: Optional[str]) -> Optional[int]:
    """'YYYY-MM' -> absolute month index (year*12 + month), or None."""
    if not month:
        return None
    try:
        y, m = month.split("-")[:2]
        return int(y) * 12 + int(m)
    except (ValueError, IndexError):
        return None


def _weights(
    records: List[EnrollwareClassRecord],
) -> Tuple[List[float], str]:
    """Per-record recency weight + the weighting basis actually used.

    When any record is dated, weight = 0.5 ** (months_old / half_life), and
    undated records inherit the oldest (smallest) weight. When nothing is
    dated, weights are uniform and the basis is reported as ``"uniform"``.
    """
    indices = [_month_index(r.month) for r in records]
    known = [i for i in indices if i is not None]
    if not known:
        return [1.0] * len(records), "uniform"
    latest = max(known)
    oldest_weight = 0.5 ** ((latest - min(known)) / _HALF_LIFE_MONTHS)
    weights = []
    for idx in indices:
        if idx is None:
            weights.append(oldest_weight)
        else:
            weights.append(0.5 ** ((latest - idx) / _HALF_LIFE_MONTHS))
    return weights, "recency"


def _weighted_mean(
    pairs: List[Tuple[float, float]],
) -> Optional[float]:
    """pairs of (value, weight) -> weighted mean, or None when empty."""
    num = sum(v * w for v, w in pairs)
    den = sum(w for _, w in pairs)
    return num / den if den > 0 else None


def _confidence(n: int) -> str:
    if n >= 12:
        return "high"
    if n >= 5:
        return "medium"
    return "low"


def _forecast_one(
    course_type: str,
    records: List[EnrollwareClassRecord],
    modeled_price: Optional[float],
) -> Dict[str, Any]:
    weights, basis = _weights(records)
    rw = list(zip(records, weights))

    enrolled_pairs = [
        (float(r.enrolled), w) for r, w in rw if r.enrolled is not None
    ]
    expected_students = _weighted_mean(enrolled_pairs)

    fill_pairs = [
        (100.0 * r.enrolled / r.capacity, w)
        for r, w in rw
        if r.enrolled is not None and r.capacity is not None and r.capacity > 0
    ]
    expected_fill = _weighted_mean(fill_pairs)

    # Revenue: prefer a recency-weighted real export price; else modeled.
    price_pairs = [(float(r.price), w) for r, w in rw if r.price is not None]
    unit_price = _weighted_mean(price_pairs)
    revenue_basis: Optional[str] = None
    if unit_price is not None:
        revenue_basis = "export_price"
    elif modeled_price:
        unit_price = modeled_price
        revenue_basis = "modeled_allcpr_median"
    expected_revenue = (
        round(expected_students * unit_price, 2)
        if expected_students is not None and unit_price is not None else None
    )

    n_enrolled = len(enrolled_pairs)
    return {
        "course_type": course_type,
        "label": COURSE_TYPE_LABELS.get(course_type, course_type),
        "sample_size": len(records),
        "expected_students": round(expected_students, 2) if expected_students is not None else None,
        "expected_fill_rate_percent": round(expected_fill, 1) if expected_fill is not None else None,
        "expected_revenue": expected_revenue,
        "revenue_basis": revenue_basis,
        "weighting": basis,
        "confidence": _confidence(n_enrolled),
        # Reserved for a future ML model — deterministic features only, no
        # leakage of the target. ``ml_ready`` flips on when a model is wired in.
        "features": {
            "weighted_avg_students": round(expected_students, 3) if expected_students is not None else None,
            "weighted_fill_rate": round(expected_fill, 3) if expected_fill is not None else None,
            "sample_size": len(records),
            "classes_with_enrollment": n_enrolled,
        },
    }


def build_forecast(
    records: List[EnrollwareClassRecord],
    modeled_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Forecast expected students / fill / revenue per course type and overall.

    ``records`` should already be filtered to the area of interest. Returns a
    JSON-serializable payload, or ``None`` when there are no records.
    """
    if not records:
        return None

    grouped: Dict[str, List[EnrollwareClassRecord]] = {}
    for r in records:
        grouped.setdefault(r.course_type, []).append(r)

    per_course = [
        _forecast_one(ct, recs, modeled_price)
        for ct, recs in grouped.items()
    ]
    per_course.sort(
        key=lambda c: (
            c["expected_students"] is not None,
            c["expected_students"] or 0.0,
        ),
        reverse=True,
    )

    overall = _forecast_one("__overall__", records, modeled_price)
    overall.pop("course_type", None)
    overall["label"] = "All course types"

    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD,
        "half_life_months": _HALF_LIFE_MONTHS,
        "ml_ready": False,
        "overall": overall,
        "course_types": per_course,
    }
