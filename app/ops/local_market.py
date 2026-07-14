"""
Local market context: real competitor class supply/pricing and real student
demand for a ZIP, loaded from the manual imports produced by
``scripts/import_enrollware_ops_data.py``.

This is *displayed context*, not a score. The operating-feasibility score is
still driven only by the enrichment pipeline (never recomputed here); these
helpers just answer two boss questions the panel could not answer before:

    "What do competitors charge for a class in this ZIP right now?"
    "How many real students came from this ZIP in the last 6 months?"

Both files are optional and gitignored (third-party + student PII). A missing
or malformed file yields empty context — never a crash — exactly like the rest
of the ops import layer.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import MANUAL_DIR
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

COMPETITOR_FILE = MANUAL_DIR / "competitor_classes.csv"
DEMAND_FILE = MANUAL_DIR / "local_demand.csv"


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    """CSV (or sibling .json) → dict rows; missing/malformed → []."""
    p = Path(path)
    json_path = p.with_suffix(".json")
    if not p.exists() and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"local_market: bad JSON {json_path}: {exc}")
            return []
    if not p.exists():
        return []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError as exc:
        logger.warning(f"local_market: could not read {p}: {exc}")
        return []


def _to_float(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    f = _to_float(value)
    return int(f) if f is not None else None


def _norm_zip(zip_code: str) -> str:
    return str(zip_code).strip().zfill(5)


def competitor_context(zip_code: str,
                       path: Path = COMPETITOR_FILE) -> Dict[str, Any]:
    """Competitor class supply + pricing for a ZIP, broken out by course.

    Shape (empty ``courses`` when nothing is on file for the ZIP):
        {"zip", "total_classes", "provider_count", "courses": [
            {"course_type", "class_count", "provider_count",
             "median_price", "min_price", "max_price", "providers",
             "sample_locations"}], "providers": [...]}
    """
    zip_code = _norm_zip(zip_code)
    courses: List[Dict[str, Any]] = []
    providers: set = set()
    total = 0
    for row in _load_rows(path):
        if _norm_zip(row.get("zip", "")) != zip_code:
            continue
        provs = [p.strip() for p in str(row.get("providers") or "").split(";")
                 if p.strip()]
        providers.update(provs)
        count = _to_int(row.get("class_count")) or 0
        total += count
        courses.append({
            "course_type": str(row.get("course_type") or "").upper(),
            "class_count": count,
            "provider_count": _to_int(row.get("provider_count")),
            "median_price": _to_float(row.get("median_price")),
            "min_price": _to_float(row.get("min_price")),
            "max_price": _to_float(row.get("max_price")),
            "providers": provs,
            "sample_locations": [
                s.strip() for s in
                str(row.get("sample_locations") or "").split(";") if s.strip()],
        })
    courses.sort(key=lambda c: c["class_count"], reverse=True)
    return {
        "zip": zip_code,
        "total_classes": total,
        "provider_count": len(providers),
        "providers": sorted(providers),
        "courses": courses,
        "has_data": bool(courses),
    }


def local_demand_context(zip_code: str,
                         path: Path = DEMAND_FILE) -> Dict[str, Any]:
    """Real 6-month student demand for a ZIP (from the student export).

    Shape:
        {"zip", "student_count", "class_count", "instructor_count",
         "top_courses": [...], "latest_registration", "has_data"}
    """
    zip_code = _norm_zip(zip_code)
    for row in _load_rows(path):
        if _norm_zip(row.get("zip", "")) != zip_code:
            continue
        top = [c.strip() for c in str(row.get("top_courses") or "").split(";")
               if c.strip()]
        return {
            "zip": zip_code,
            "student_count": _to_int(row.get("student_count")) or 0,
            "class_count": _to_int(row.get("class_count")) or 0,
            "instructor_count": _to_int(row.get("instructor_count")) or 0,
            "top_courses": top,
            "latest_registration": str(row.get("latest_registration") or ""),
            "has_data": True,
        }
    return {
        "zip": zip_code,
        "student_count": 0,
        "class_count": 0,
        "instructor_count": 0,
        "top_courses": [],
        "latest_registration": "",
        "has_data": False,
    }


def local_market_context(zip_code: str) -> Dict[str, Any]:
    """Both context blocks for a ZIP, for the readiness payload/panel."""
    return {
        "competitor": competitor_context(zip_code),
        "demand": local_demand_context(zip_code),
    }
