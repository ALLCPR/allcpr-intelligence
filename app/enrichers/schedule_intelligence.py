"""
Schedule intelligence (STEP 8).

Learns *when* ALLCPR's classes have actually performed best from real Enrollware
history and emits deterministic scheduling recommendations:

  - best day of week
  - best calendar month
  - weekend vs weekday
  - best time of day  (honestly ``None`` — the export does not carry class time)

Pure and deterministic. Rankings prefer average enrollment; when no class
carries an enrollment count we fall back to class *volume* (how often ALLCPR
historically scheduled that slot) and label the basis as ``"volume"`` so the
report never presents "most scheduled" as "best attended". Returns ``None``
when there is no dated history to learn from.
"""
from __future__ import annotations

import calendar
from datetime import datetime
from statistics import mean
from typing import Any, Dict, List, Optional

from app.collectors.enrollware import EnrollwareClassRecord

_WEEKDAY_NAMES = list(calendar.day_name)      # Monday .. Sunday
_MONTH_NAMES = list(calendar.month_name)      # ['', January, .. December]

# A slot needs at least this many classes before we will call it "best".
_MIN_SAMPLE = 2


def _weekday(record: EnrollwareClassRecord) -> Optional[int]:
    if not record.date:
        return None
    try:
        return datetime.strptime(record.date, "%Y-%m-%d").weekday()
    except ValueError:
        return None


def _month_num(record: EnrollwareClassRecord) -> Optional[int]:
    if not record.month:
        return None
    try:
        return int(record.month.split("-")[1])
    except (IndexError, ValueError):
        return None


def _best_bucket(
    buckets: Dict[Any, List[EnrollwareClassRecord]],
    label_of,
) -> Optional[Dict[str, Any]]:
    """Pick the best-performing bucket.

    Prefers highest average enrollment among buckets meeting the sample floor;
    falls back to highest class volume when no enrollment is known anywhere.
    """
    if not buckets:
        return None

    enriched: List[Dict[str, Any]] = []
    any_enrollment = False
    for key, recs in buckets.items():
        enrolled = [r.enrolled for r in recs if r.enrolled is not None]
        if enrolled:
            any_enrollment = True
        enriched.append({
            "key": key,
            "label": label_of(key),
            "classes": len(recs),
            "average_students_per_class": round(mean(enrolled), 2) if enrolled else None,
        })

    eligible = [b for b in enriched if b["classes"] >= _MIN_SAMPLE] or enriched

    if any_enrollment:
        scored = [b for b in eligible if b["average_students_per_class"] is not None]
        if scored:
            best = max(scored, key=lambda b: (b["average_students_per_class"], b["classes"]))
            best["basis"] = "enrollment"
            return best
    # Volume fallback.
    best = max(eligible, key=lambda b: b["classes"])
    best["basis"] = "volume"
    return best


def _day_part_summary(records: List[EnrollwareClassRecord]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for part in ("weekday", "weekend"):
        subset = [r for r in records if r.day_part == part]
        enrolled = [r.enrolled for r in subset if r.enrolled is not None]
        out[part] = {
            "classes": len(subset),
            "average_students_per_class": round(mean(enrolled), 2) if enrolled else None,
        }
    return out


def build_schedule_intelligence(
    records: List[EnrollwareClassRecord],
) -> Optional[Dict[str, Any]]:
    """Learn best day / month / day-part from dated Enrollware history.

    Returns ``{best_day, best_month, weekend_vs_weekday, best_time,
    recommendations}`` or ``None`` when no record carries a usable date.
    """
    dated = [r for r in records if r.date]
    if not dated:
        return None

    # Bucket by weekday and calendar month.
    by_weekday: Dict[int, List[EnrollwareClassRecord]] = {}
    for r in dated:
        wd = _weekday(r)
        if wd is not None:
            by_weekday.setdefault(wd, []).append(r)
    by_month: Dict[int, List[EnrollwareClassRecord]] = {}
    for r in dated:
        mn = _month_num(r)
        if mn is not None:
            by_month.setdefault(mn, []).append(r)

    best_day = _best_bucket(by_weekday, lambda k: _WEEKDAY_NAMES[k])
    best_month = _best_bucket(by_month, lambda k: _MONTH_NAMES[k])
    day_part = _day_part_summary(dated)

    recommendations: List[str] = []

    if best_day:
        if best_day["basis"] == "enrollment":
            recommendations.append(
                f"{best_day['label']} classes attract the most students "
                f"(avg {best_day['average_students_per_class']:.1f} over "
                f"{best_day['classes']} class(es)) — favor {best_day['label']}s."
            )
        else:
            recommendations.append(
                f"{best_day['label']} is your most-scheduled day "
                f"({best_day['classes']} class(es)); enrollment isn't recorded, "
                f"so this is by volume, not attendance."
            )

    if best_month and best_month["basis"] == "enrollment":
        recommendations.append(
            f"{best_month['label']} has historically filled best "
            f"(avg {best_month['average_students_per_class']:.1f} students/class)."
        )

    wd = day_part["weekday"]["average_students_per_class"]
    we = day_part["weekend"]["average_students_per_class"]
    if wd is not None and we is not None:
        if we >= wd * 1.15:
            recommendations.append(
                f"Weekends fill better (avg {we:.1f} vs weekday {wd:.1f}) — "
                f"add weekend sessions."
            )
        elif wd >= we * 1.15:
            recommendations.append(
                f"Weekdays fill better (avg {wd:.1f} vs weekend {we:.1f}) — "
                f"test more weekday evening sessions."
            )
        else:
            recommendations.append(
                "Weekday and weekend enrollment are comparable — schedule for "
                "instructor and room availability."
            )

    if not recommendations:
        recommendations.append(
            "Not enough dated/enrollment history to recommend a schedule — "
            "collect more class records."
        )

    return {
        "best_day": best_day,
        "best_month": best_month,
        "weekend_vs_weekday": day_part,
        # The Enrollware export drops class start times, so time-of-day cannot
        # be learned. Kept explicit so the report can say "unknown", not guess.
        "best_time": None,
        "best_time_note": "Class start time is not in the Enrollware export.",
        "recommendations": recommendations,
    }
