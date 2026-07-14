"""
Instructor performance track record (from the 6-month student export).

Loads the per-instructor aggregate produced by
``scripts/import_enrollware_ops_data.py`` and turns it into a comparable
performance score + tier. This is what lets the sourcing engine answer
"who are our best instructors, and what do they have in common?" — the basis
for finding *new* instructors who look like the proven ones.

Honesty note: what an instructor has actually taught is treated as proof of
their credential/authorization for that course type — teaching an AHA BLS
Provider course means they hold a current AHA BLS Instructor credential; a
Red Cross course means a current Red Cross Instructor authorization. This is
stronger evidence than a roster id, and it is what ``proven_courses`` reports.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import MANUAL_DIR
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED
from app.ops.recruiting_policy import company_grade
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

PERFORMANCE_FILE = MANUAL_DIR / "instructor_performance.csv"

# Benchmarks that map a raw metric onto its full share of the score. Derived
# from the real top-performer distribution (a top instructor runs ~500
# students / 6mo, ~8 students per class, across ~120 student ZIPs).
_STUDENTS_FULL = 500.0
_FILL_FULL = 8.0
_REACH_FULL = 120.0

# Score weights (sum 100 before recency, which is a multiplier band).
_W_VOLUME = 45.0
_W_FILL = 25.0
_W_REACH = 15.0
_W_MIX = 15.0   # teaching more than one product line = more schedulable

TIER_TOP = "Top Performer"
TIER_SOLID = "Solid"
TIER_DEVELOPING = "Developing"
TIER_LOW = "Low Volume"


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date_key(value: Any) -> Tuple[int, int, int]:
    """Sortable (y, m, d) from 'M/D/YY', 'M/D/YYYY', or ISO; else (0,0,0)."""
    import re
    text = str(value or "").split()[0] if value else ""
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    mdy = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if mdy:
        year = int(mdy.group(3)) + (2000 if int(mdy.group(3)) < 100 else 0)
        return (year, int(mdy.group(1)), int(mdy.group(2)))
    return (0, 0, 0)


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    p = Path(path)
    json_path = p.with_suffix(".json")
    if not p.exists() and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"instructor_performance: bad JSON {json_path}: {exc}")
            return []
    if not p.exists():
        return []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError as exc:
        logger.warning(f"instructor_performance: could not read {p}: {exc}")
        return []


def proven_courses(row: Dict[str, Any]) -> List[str]:
    """Course types this instructor has actually taught (= is authorized for)."""
    out: List[str] = []
    if _to_int(row.get("aha_bls_students")) > 0:
        out.append(AHA_BLS)
    if _to_int(row.get("arc_bls_students")) > 0:
        out.append(ARC_BLS)
    if _to_int(row.get("arc_cpr_students")) > 0:
        out.append(ARC_CPR_FA_AED)
    return out


def performance_score(row: Dict[str, Any]) -> float:
    """0..100 composite: volume + fill rate + reach + product-line breadth."""
    students = _to_int(row.get("students"))
    fill = _to_float(row.get("students_per_class")) or 0.0
    reach = _to_int(row.get("zips"))
    lines = len(proven_courses(row))

    volume = min(1.0, students / _STUDENTS_FULL) * _W_VOLUME
    fill_pts = min(1.0, fill / _FILL_FULL) * _W_FILL
    reach_pts = min(1.0, reach / _REACH_FULL) * _W_REACH
    mix_pts = min(1.0, lines / 3.0) * _W_MIX
    return round(volume + fill_pts + reach_pts + mix_pts, 1)


def performance_tier(score: float) -> str:
    if score >= 70:
        return TIER_TOP
    if score >= 45:
        return TIER_SOLID
    if score >= 25:
        return TIER_DEVELOPING
    return TIER_LOW


def enrich(row: Dict[str, Any]) -> Dict[str, Any]:
    """Attach parsed metrics, proven courses, score, and tier to a raw row."""
    score = performance_score(row)
    return {
        "name": str(row.get("name") or "").strip(),
        "students": _to_int(row.get("students")),
        "classes": _to_int(row.get("classes")),
        "zips": _to_int(row.get("zips")),
        "students_per_class": _to_float(row.get("students_per_class")),
        "aha_bls_students": _to_int(row.get("aha_bls_students")),
        "arc_bls_students": _to_int(row.get("arc_bls_students")),
        "arc_cpr_students": _to_int(row.get("arc_cpr_students")),
        "teaches_aha": str(row.get("teaches_aha") or "").lower() == "yes"
        or _to_int(row.get("aha_bls_students")) > 0,
        "teaches_arc": str(row.get("teaches_arc") or "").lower() == "yes"
        or (_to_int(row.get("arc_bls_students"))
            + _to_int(row.get("arc_cpr_students"))) > 0,
        "home_city": str(row.get("home_city") or "").strip(),
        "home_state": str(row.get("home_state") or "").strip(),
        "home_zip": str(row.get("home_zip") or "").strip(),
        "last_taught": str(row.get("last_taught") or "").strip(),
        "last_taught_key": _date_key(row.get("last_taught")),
        "proven_courses": proven_courses(row),
        "performance_score": score,
        "performance_tier": performance_tier(score),
        # ALLCPR's own A–E grade band applied to the performance score.
        "company_grade": company_grade(score)["grade"],
    }


def load_instructor_performance(path: Path = PERFORMANCE_FILE
                                ) -> List[Dict[str, Any]]:
    """All instructors with performance metrics, best first (missing → [])."""
    rows = [enrich(r) for r in _load_rows(path) if str(r.get("name") or "").strip()]
    rows.sort(key=lambda r: r["performance_score"], reverse=True)
    return rows


def top_performers(rows: List[Dict[str, Any]], course: Optional[str] = None,
                   limit: int = 10) -> List[Dict[str, Any]]:
    """Best instructors overall, or best at a given course type."""
    pool = rows
    if course:
        pool = [r for r in rows if course in r["proven_courses"]]
    return sorted(pool, key=lambda r: r["performance_score"],
                  reverse=True)[:limit]
