"""
Candidate-level Enrollware historical performance.

This layer turns ALLCPR's real class history into a per-candidate evidence
payload before site scoring runs. It answers:

  - how many classes ALLCPR has run in this city/location
  - enrollment and fill signals when present
  - course type frequency
  - recent activity, measured against the latest class in the export
  - which historical cities look strong or weak relative to ALLCPR overall

No history is a neutral signal for scoring (50/100), not a penalty. A location
only gets boosted or penalized when enough real Enrollware records exist.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

from app.collectors.enrollware import (
    COURSE_TYPE_LABELS,
    EnrollwareClassRecord,
    held_classes,
)
from app.enrichers.course_performance import rollup_decision_records
from app.scoring.historical_performance_score import score_historical_performance

NEUTRAL_HISTORICAL_SCORE = 50.0


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _avg(values: Iterable[float]) -> Optional[float]:
    vals = list(values)
    return round(sum(vals) / len(vals), 2) if vals else None


def _fill_rate(records: List[EnrollwareClassRecord]) -> Optional[float]:
    pairs = [
        (r.enrolled, r.capacity) for r in records
        if r.enrolled is not None and r.capacity is not None and r.capacity > 0
    ]
    if not pairs:
        return None
    filled = sum(e for e, _ in pairs)
    capacity = sum(c for _, c in pairs)
    return round(100.0 * filled / capacity, 1) if capacity > 0 else None


def _match_area_records(
    records: List[EnrollwareClassRecord],
    city: Optional[str] = None,
    state: Optional[str] = None,
    location: Optional[str] = None,
) -> tuple[List[EnrollwareClassRecord], str]:
    city_n = _norm(city)
    state_n = _norm(state)
    loc_n = _norm(location)
    city_aliases = [city_n] if city_n else []
    if city and "," in str(city):
        parts = [_norm(p) for p in str(city).split(",") if _norm(p)]
        # Targets often look like "Santana Row, San Jose"; Enrollware is
        # city-level, so the final comma segment is the safest fallback.
        for alias in reversed(parts):
            if alias not in city_aliases:
                city_aliases.append(alias)

    def state_ok(r: EnrollwareClassRecord) -> bool:
        return not state_n or _norm(r.state) == state_n

    if loc_n:
        matched = [
            r for r in records
            if state_ok(r) and (_norm(r.location) == loc_n or _norm(r.city) == loc_n)
        ]
        if matched:
            return matched, "location"

    for alias in city_aliases:
        matched = [
            r for r in records
            if state_ok(r) and (_norm(r.city) == alias or _norm(r.location) == alias)
        ]
        if matched:
            return matched, "city" if alias == city_n else "city_alias"

        # Some Enrollware locations include a course suffix, for example
        # "San Jose AHA BLS". Use a conservative prefix fallback only after
        # exact city/location matches fail.
        matched = [
            r for r in records
            if state_ok(r) and _norm(r.location).startswith(alias + " ")
        ]
        if matched:
            return matched, "location_prefix"

    return [], "none"


def _course_frequency(records: List[EnrollwareClassRecord]) -> List[Dict[str, Any]]:
    decision_records, _ = rollup_decision_records(records)
    counts = Counter(r.course_type for r in decision_records)
    out = []
    for course_type, count in counts.most_common():
        out.append({
            "course_type": course_type,
            "label": COURSE_TYPE_LABELS.get(course_type, course_type),
            "classes": count,
        })
    return out


def _count_by(records: List[EnrollwareClassRecord], field: str) -> List[Dict[str, Any]]:
    counts: Counter[str] = Counter()
    for r in records:
        value = getattr(r, field, None)
        key = str(value or "Unknown").strip() or "Unknown"
        counts[key] += 1
    return [{"name": k, "classes": v} for k, v in counts.most_common(12)]


def _recent_activity(records: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    dates = [d for d in (_parse_date(r.date) for r in records) if d is not None]
    if not dates:
        return {
            "latest_class_date": None,
            "classes_last_180_days": None,
            "basis": "no_dates",
        }
    latest = max(dates)
    cutoff = latest - timedelta(days=180)
    return {
        "latest_class_date": latest.strftime("%Y-%m-%d"),
        "classes_last_180_days": sum(1 for d in dates if d >= cutoff),
        "basis": "relative_to_latest_export_date",
    }


def _overall_reference_avg(records: List[EnrollwareClassRecord]) -> Optional[float]:
    vals = [float(r.enrolled) for r in records if r.enrolled and r.enrolled > 0]
    return _avg(vals)


def _strong_weak_locations(
    records: List[EnrollwareClassRecord],
    reference_avg: Optional[float],
) -> Dict[str, List[Dict[str, Any]]]:
    if not reference_avg:
        return {"strong": [], "weak": []}

    grouped: Dict[str, List[EnrollwareClassRecord]] = defaultdict(list)
    for r in records:
        key = r.city or r.location
        if key:
            grouped[str(key)].append(r)

    scored = []
    for key, recs in grouped.items():
        enrolled = [float(r.enrolled) for r in recs if r.enrolled is not None]
        if len(enrolled) < 3:
            continue
        avg_students = _avg(enrolled)
        if avg_students is None:
            continue
        scored.append({
            "name": key,
            "classes": len(recs),
            "average_students_per_class": avg_students,
            "delta_vs_allcpr_avg": round(avg_students - reference_avg, 2),
        })

    strong = [
        row for row in scored
        if row["classes"] >= 5
        and row["average_students_per_class"] >= reference_avg * 1.15
    ]
    weak = [
        row for row in scored
        if row["classes"] >= 5
        and row["average_students_per_class"] <= reference_avg * 0.85
    ]
    strong.sort(key=lambda r: (r["average_students_per_class"], r["classes"]), reverse=True)
    weak.sort(key=lambda r: (r["average_students_per_class"], -r["classes"]))
    return {"strong": strong[:8], "weak": weak[:8]}


def build_candidate_historical_performance(
    records: List[EnrollwareClassRecord],
    city: Optional[str] = None,
    state: Optional[str] = None,
    location: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a JSON-serializable historical performance payload.

    ``score`` is always 0..100 when records exist. If there is no matching or
    insufficient local history it is neutral (50) and marked as such.
    """
    if not records:
        return None

    # Use the same held-class basis as the benchmark + trend sections: completed
    # months only, real attendance only. Future scheduled classes and
    # zero-enrollment placeholder rows are NOT historical performance and would
    # otherwise deflate averages/fill and fake a "trending down" signal.
    records = held_classes(records)
    if not records:
        return None

    area_records, match_type = _match_area_records(
        records, city=city, state=state, location=location,
    )
    reference_avg = _overall_reference_avg(records)
    strong_weak = _strong_weak_locations(records, reference_avg)

    if not area_records:
        label = ", ".join(p for p in (city, state) if p) or location or "unknown"
        return {
            "area_label": label,
            "match_type": match_type,
            "status": "no_matching_history",
            "score": NEUTRAL_HISTORICAL_SCORE,
            "confidence": "none",
            "total_classes": 0,
            "total_students": None,
            "average_students_per_class": None,
            "fill_rate_percent": None,
            "course_type_frequency": [],
            "recent_activity": None,
            "course_count_by_city": _count_by(records, "city"),
            "course_count_by_location": _count_by(records, "location"),
            "strong_locations": strong_weak["strong"],
            "weak_locations": strong_weak["weak"],
            "reasons": ["No matching ALLCPR class history for this city/location."],
        }

    enrolled = [float(r.enrolled) for r in area_records if r.enrolled is not None]
    hist_score = score_historical_performance(
        area_records, reference_avg=reference_avg
    )
    score = (
        hist_score["score"] if hist_score is not None
        else NEUTRAL_HISTORICAL_SCORE
    )
    confidence = hist_score["confidence"] if hist_score is not None else "low"
    reasons = hist_score["reasons"] if hist_score is not None else [
        "Matching ALLCPR history exists, but sample size is too small to move the score."
    ]
    status = "scored" if hist_score is not None else "insufficient_history"
    area_label = ", ".join(p for p in (city, state) if p) or location or "matched area"

    return {
        "area_label": area_label,
        "match_type": match_type,
        "status": status,
        "score": round(float(score), 1),
        "confidence": confidence,
        "total_classes": len(area_records),
        "total_students": int(sum(enrolled)) if enrolled else None,
        "average_students_per_class": _avg(enrolled),
        "fill_rate_percent": _fill_rate(area_records),
        "course_type_frequency": _course_frequency(area_records),
        "recent_activity": _recent_activity(area_records),
        "course_count_by_city": _count_by(records, "city"),
        "course_count_by_location": _count_by(records, "location"),
        "strong_locations": strong_weak["strong"],
        "weak_locations": strong_weak["weak"],
        "reasons": reasons,
        "components": hist_score.get("components") if hist_score else {},
        "allcpr_average_students_per_class": reference_avg,
    }
