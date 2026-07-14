"""
Manual ALLCPR data imports for the expansion-operations layer.

Same philosophy as ``app/reports/commercial_validation.py``: hand-maintained
CSV (or JSON) files under ``data/manual/``, header-driven, extras ignored, a
missing or malformed file never crashes — it just yields nothing. Tracked
``*.example.csv`` files document each format; the real files are gitignored
because they carry internal ALLCPR data.

Files (CSV preferred; a same-stem ``.json`` list of objects also works):
    data/manual/allcpr_instructors.csv   Existing instructor roster
    data/manual/allcpr_locations.csv     Current/former ALLCPR locations
    data/manual/course_economics.csv     Per-course pricing/cost assumptions
    data/manual/room_budget_rules.csv    Room budget policy (single row)
    data/manual/credential_rules.csv     Per-course credential approval rules
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import MANUAL_DIR
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

INSTRUCTORS_FILE = MANUAL_DIR / "allcpr_instructors.csv"
LOCATIONS_FILE = MANUAL_DIR / "allcpr_locations.csv"
COURSE_ECONOMICS_FILE = MANUAL_DIR / "course_economics.csv"
ROOM_BUDGET_RULES_FILE = MANUAL_DIR / "room_budget_rules.csv"
CREDENTIAL_RULES_FILE = MANUAL_DIR / "credential_rules.csv"

# The real source CSVs an operator may upload to a hosted instance's persistent
# disk (POST /api/ops/admin/import-manual / scripts/push_manual_data.py) so the
# matching/coverage/revenue engines have data to read. Whitelisted so the
# upload endpoint can only ever write these known filenames (no path traversal,
# no arbitrary writes).
MANUAL_CSV_WHITELIST = frozenset({
    "allcpr_instructors.csv", "allcpr_locations.csv", "course_economics.csv",
    "room_budget_rules.csv", "credential_rules.csv", "competitor_classes.csv",
    "local_demand.csv", "commercial_validation.csv",
    "instructor_performance.csv", "site_revenue_health.csv",
    "site_economics.csv",
})


def save_manual_csv(filename: str, content: str):
    """Write one whitelisted manual CSV to MANUAL_DIR. Returns (ok, reason)."""
    name = Path(str(filename)).name          # strip any directory component
    if name not in MANUAL_CSV_WHITELIST:
        return False, "not_whitelisted"
    if not isinstance(content, str):
        return False, "content_must_be_text"
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    (MANUAL_DIR / name).write_text(content, encoding="utf-8")
    return True, "ok"

_TRUTHY = {"yes", "y", "true", "1", "required", "confirmed"}
_FALSY = {"no", "n", "false", "0", "not required", "optional"}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_float(value: Any) -> Optional[float]:
    text = _clean(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_bool(value: Any) -> Optional[bool]:
    text = _clean(value).lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def parse_list(value: Any) -> List[str]:
    """Split a semicolon/pipe-delimited cell into cleaned items."""
    text = _clean(value)
    if not text:
        return []
    for sep in (";", "|"):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    """Read a CSV (or sibling ``.json``) into dict rows. Missing → []."""
    p = Path(path)
    json_path = p.with_suffix(".json")
    if not p.exists() and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"ops import: bad JSON {json_path}: {exc}")
            return []
    if not p.exists():
        return []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError as exc:
        logger.warning(f"ops import: could not read {p}: {exc}")
        return []


def load_instructors_import(path: Path = INSTRUCTORS_FILE
                            ) -> List[Dict[str, Any]]:
    """Existing ALLCPR instructor roster.

    Columns: name, email, phone, city, state, zip, courses, certifications,
    expiration_dates, travel_radius_miles, availability, pay_rate,
    reliability_notes, languages, long_term_interest, verified

    ``email``/``phone`` are optional; when present they make a roster row a
    real "who to contact" lead (the ops layer already rewards known contact
    info). They are ALLCPR-internal contact fields — served only to staff and
    scrubbed of any operational secrets by ``scrub_sensitive`` at the API edge.
    """
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(_load_rows(path), start=2):
        name = _clean(raw.get("name"))
        if not name:
            logger.warning(f"allcpr_instructors row {i}: missing name; skipped")
            continue
        out.append({
            "name": name,
            "email": _clean(raw.get("email")),
            "phone": _clean(raw.get("phone")),
            "city": _clean(raw.get("city")),
            "state": _clean(raw.get("state")),
            "zip": _clean(raw.get("zip")).zfill(5) if _clean(raw.get("zip")) else "",
            "courses": parse_list(raw.get("courses")),
            "certifications": parse_list(raw.get("certifications")),
            "expiration_dates": parse_list(raw.get("expiration_dates")),
            "travel_radius_miles": parse_float(raw.get("travel_radius_miles")),
            "availability": _clean(raw.get("availability")),
            "pay_rate": _clean(raw.get("pay_rate")),
            "reliability_notes": _clean(raw.get("reliability_notes")),
            "languages": parse_list(raw.get("languages")),
            "long_term_interest": _clean(raw.get("long_term_interest")).upper()
            or "UNKNOWN",
            # Credential honesty: only a human-set verified flag upgrades a
            # roster row past NEEDS_VERIFICATION.
            "verified": parse_bool(raw.get("verified")) or False,
        })
    return out


def load_locations_import(path: Path = LOCATIONS_FILE) -> List[Dict[str, Any]]:
    """Current/former ALLCPR locations.

    Columns: location_name, address, zip, active_status, courses_offered,
    capacity, rent_cost, parking_notes, room_notes, average_monthly_enrollment
    """
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(_load_rows(path), start=2):
        name = _clean(raw.get("location_name"))
        if not name:
            logger.warning(f"allcpr_locations row {i}: missing name; skipped")
            continue
        out.append({
            "location_name": name,
            "address": _clean(raw.get("address")),
            "zip": _clean(raw.get("zip")).zfill(5) if _clean(raw.get("zip")) else "",
            "active_status": _clean(raw.get("active_status")).lower(),
            "courses_offered": parse_list(raw.get("courses_offered")),
            "capacity": parse_float(raw.get("capacity")),
            "rent_cost": parse_float(raw.get("rent_cost")),
            "parking_notes": _clean(raw.get("parking_notes")),
            "room_notes": _clean(raw.get("room_notes")),
            "average_monthly_enrollment": parse_float(
                raw.get("average_monthly_enrollment")),
        })
    return out


def load_course_economics(path: Path = COURSE_ECONOMICS_FILE
                          ) -> Dict[str, Dict[str, Any]]:
    """Per-course economics keyed by course_type.

    Columns: course_type, student_price, card_cost, instructor_cost,
    room_cost_assumption, minimum_students_break_even, target_students
    """
    out: Dict[str, Dict[str, Any]] = {}
    for raw in _load_rows(path):
        course = _clean(raw.get("course_type")).upper()
        if not course:
            continue
        out[course] = {
            "course_type": course,
            "student_price": parse_float(raw.get("student_price")),
            "card_cost": parse_float(raw.get("card_cost")),
            "instructor_cost": parse_float(raw.get("instructor_cost")),
            "room_cost_assumption": parse_float(raw.get("room_cost_assumption")),
            "minimum_students_break_even": parse_float(
                raw.get("minimum_students_break_even")),
            "target_students": parse_float(raw.get("target_students")),
        }
    return out


DEFAULT_ROOM_BUDGET_RULES: Dict[str, Any] = {
    # Conservative defaults used until ALLCPR provides real policy numbers;
    # every number is overridable via data/manual/room_budget_rules.csv.
    "max_hourly_rate": 75.0,
    "max_daily_rate": 400.0,
    "minimum_capacity": 8,
    "weekend_required": True,
    "evening_required": False,
    "parking_required": True,
    "recurring_required": True,
    "source": "default_assumptions",
}


def load_room_budget_rules(path: Path = ROOM_BUDGET_RULES_FILE
                           ) -> Dict[str, Any]:
    """Room budget policy (first row wins).

    Columns: max_hourly_rate, max_daily_rate, minimum_capacity,
    weekend_required, evening_required, parking_required, recurring_required
    """
    rows = _load_rows(path)
    if not rows:
        return dict(DEFAULT_ROOM_BUDGET_RULES)
    raw = rows[0]
    rules = dict(DEFAULT_ROOM_BUDGET_RULES)
    for key in ("max_hourly_rate", "max_daily_rate", "minimum_capacity"):
        val = parse_float(raw.get(key))
        if val is not None:
            rules[key] = val
    for key in ("weekend_required", "evening_required", "parking_required",
                "recurring_required"):
        val = parse_bool(raw.get(key))
        if val is not None:
            rules[key] = val
    rules["source"] = "manual_import"
    return rules


def load_credential_rules(path: Path = CREDENTIAL_RULES_FILE
                          ) -> Dict[str, Dict[str, Any]]:
    """Per-course credential approval rules keyed by course_type.

    Columns: course_type, required_documents, approval_owner,
    verification_notes
    """
    out: Dict[str, Dict[str, Any]] = {}
    for raw in _load_rows(path):
        course = _clean(raw.get("course_type")).upper()
        if not course:
            continue
        out[course] = {
            "course_type": course,
            "required_documents": parse_list(raw.get("required_documents")),
            "approval_owner": _clean(raw.get("approval_owner")),
            "verification_notes": _clean(raw.get("verification_notes")),
        }
    return out
