"""
Enrollware class-history loader + course-type normalizer (Phase 4B).

ALLCPR's real class behavior lives in Enrollware exports (one row per scheduled
class, with enrollment counts, dates, locations and — sometimes — capacity and
price). This module turns a messy export into a list of normalized
``EnrollwareClassRecord`` objects the course-performance enricher can aggregate.

Design rules (consistent with the rest of the pipeline):
  - **Never invent.** Enrollment, capacity, price and date stay ``None`` when
    the export does not contain them. Downstream code labels those "unknown".
  - **Deterministic classification first.** ``classify_course_type`` maps a
    messy class name onto a small fixed catalog (ARC CPR, ARC BLS, AHA BLS,
    ALLCPR BLS, blended CPR/First Aid, Skills sessions, plus a few other
    detected provider/course combos). Anything we cannot confidently classify
    becomes ``unknown_course_type`` rather than being forced into a bucket.
  - **Graceful absence.** When no Enrollware file is present the loader returns
    ``[]`` and the rest of the pipeline simply omits the course-performance
    sections — exactly like the proprietary price/override loaders.

File format: ``.xlsx`` / ``.xls`` (read via pandas + openpyxl when available)
or ``.csv`` (read with the stdlib, no extra dependency). The real export may
live at ``data/raw/Enrollware Data - Classes.xlsx`` or
``data/raw/enrollware_classes.{xlsx,csv}``; both are gitignored. A committed
``enrollware_classes.example.csv`` documents the expected columns.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import RAW_DIR
from app.enrichers.course_classifier import (  # canonical taxonomy + classifier
    COURSE_TYPE_LABELS,
    _norm,
    classify_course_type,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Re-exported for backward compatibility: callers and tests import these from
# ``app.collectors.enrollware``. The single source of truth is
# ``app.enrichers.course_classifier``.
__all__ = [
    "COURSE_TYPE_LABELS",
    "classify_course_type",
    "EnrollwareClassRecord",
    "load_records",
    "load_locations",
]

# Candidate file locations, tried in order. The real files are proprietary and
# gitignored; the .example.csv ships so the schema is discoverable.
ENROLLWARE_FILES: List[Path] = [
    RAW_DIR / "Enrollware Data - Classes.xlsx",
    RAW_DIR / "Enrollware Data - Classes.xls",
    RAW_DIR / "Enrollware Data - Classes.csv",
    RAW_DIR / "enrollware_classes.xlsx",
    RAW_DIR / "enrollware_classes.xls",
    RAW_DIR / "enrollware_classes.csv",
    RAW_DIR / "enrollware_classes.example.csv",
]

# Locations export — maps a class "Location" (an Abbreviation) to a real
# address from which we parse city + state. Optional: when absent, the class
# "Location" value is used as-is.
LOCATIONS_FILES: List[Path] = [
    RAW_DIR / "Enrollware Data - Locations.xlsx",
    RAW_DIR / "Enrollware Data - Locations.xls",
    RAW_DIR / "Enrollware Data - Locations.csv",
    RAW_DIR / "enrollware_locations.xlsx",
    RAW_DIR / "enrollware_locations.xls",
    RAW_DIR / "enrollware_locations.csv",
]


# --------------------------------------------------------------------------- #
# Record model
# --------------------------------------------------------------------------- #

@dataclass
class EnrollwareClassRecord:
    """One scheduled class. Any unknown field stays None — never imputed."""
    class_name: str
    course_type: str
    course_type_label: str
    date: Optional[str] = None          # ISO YYYY-MM-DD when parseable
    day_part: Optional[str] = None      # "weekday" | "weekend"
    month: Optional[str] = None         # YYYY-MM bucket for trend
    enrolled: Optional[int] = None
    capacity: Optional[int] = None
    price: Optional[float] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None           # from the Locations join; never invented
    location: Optional[str] = None
    status: Optional[str] = None
    cancelled: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# "Held class" filter — the honest basis for every historical aggregate
# --------------------------------------------------------------------------- #
#
# The Enrollware export carries NO status/cancelled column (only Students and
# Seats), so the pipeline cannot read a cancellation flag — every record's
# ``cancelled`` is None. That makes enrollment the only honest signal for
# "did this class actually run":
#   - ``enrolled in (None, 0)``  → never ran (cancelled / placeholder / no-show).
#     ~32% of rows are zero-enrolled, and 88% of past zero-enrolled rows also
#     have 0 seats — phantom rows that would otherwise tank every average.
#   - a class in the current or a future month has not realised its enrollment
#     yet (people are still signing up), so counting its low/zero count would
#     fake a decline.
# A class is therefore "held" — and countable in averages, benchmarks, trends
# and scoring — only when it ran with real attendance in a completed month.

def held_class_cutoff_month(today: Optional[date] = None) -> str:
    """Current calendar month as ``YYYY-MM`` — the exclusive completed-month
    cutoff (classes in this month or later are not yet realised history)."""
    t = today or date.today()
    return f"{t.year:04d}-{t.month:02d}"


def is_held_class(r: "EnrollwareClassRecord",
                  cutoff_month: Optional[str] = None) -> bool:
    """True when a class actually ran with real attendance in a completed month.

    Excludes cancelled (when ever knowable), zero/blank enrollment, and
    future/current-partial months. Undated classes are kept when they carry real
    enrollment, since we cannot place them on the timeline to call them future.
    """
    if r.enrolled is None or r.enrolled <= 0:
        return False
    if r.cancelled is True:
        return False
    cutoff = cutoff_month or held_class_cutoff_month()
    month = r.month or (r.date[:7] if r.date else None)
    if month is not None and month >= cutoff:
        return False
    return True


def held_classes(records: List["EnrollwareClassRecord"],
                 today: Optional[date] = None) -> List["EnrollwareClassRecord"]:
    """Filter to the held classes that belong in historical aggregates."""
    cutoff = held_class_cutoff_month(today)
    return [r for r in records if is_held_class(r, cutoff)]


# --------------------------------------------------------------------------- #
# Column detection — Enrollware exports have inconsistent headers
# --------------------------------------------------------------------------- #

# Canonical field -> ordered list of header aliases (normalized, exact-first
# then substring). The first column whose normalized header matches wins.
_COLUMN_ALIASES: Dict[str, List[str]] = {
    "class_name": ["class name", "course name", "class", "course", "class type",
                   "course type", "title", "event", "name"],
    "date": ["class date", "start date", "event date", "scheduled date",
             "date", "start", "scheduled", "datetime"],
    "enrolled": ["students enrolled", "num enrolled", "# enrolled", "enrolled",
                 "enrollment", "students", "registered", "attendees",
                 "seats filled", "filled", "attendance", "count"],
    "capacity": ["max students", "max capacity", "max enrollment", "class size",
                 "capacity", "max", "seats", "limit", "max seats"],
    "price": ["price", "cost", "fee", "tuition", "amount", "rate", "list price"],
    "city": ["city", "town"],
    "location": ["location", "training center", "venue", "site", "place",
                 "address", "facility"],
    "status": ["class status", "status", "state"],
    # Detected only so the data-quality summary can count blanks; not stored on
    # the record (the pipeline does not use end-time or hours).
    "end_date": ["end date / time", "end date", "end time", "end"],
    "hours": ["hours", "hrs", "duration", "length"],
}


def _build_header_map(headers: List[str]) -> Dict[str, str]:
    """Resolve canonical field -> actual header. Exact matches beat substrings."""
    norm_headers = {h: _norm(h) for h in headers}
    resolved: Dict[str, str] = {}
    used: set = set()
    # Pass 1: exact alias matches.
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            match = next(
                (h for h, nh in norm_headers.items()
                 if nh == alias and h not in used),
                None,
            )
            if match:
                resolved[field] = match
                used.add(match)
                break
    # Pass 2: substring matches for anything still unresolved.
    for field, aliases in _COLUMN_ALIASES.items():
        if field in resolved:
            continue
        for alias in aliases:
            match = next(
                (h for h, nh in norm_headers.items()
                 if alias in nh and h not in used),
                None,
            )
            if match:
                resolved[field] = match
                used.add(match)
                break
    return resolved


# --------------------------------------------------------------------------- #
# Field parsers
# --------------------------------------------------------------------------- #

def _is_blank(value: Any) -> bool:
    """True for None / empty / whitespace-only / pandas-NaN-ish cells."""
    if value is None:
        return True
    s = str(value).strip()
    return s == "" or s.lower() in ("na", "n/a", "none", "nan", "null")


# Trailing location qualifiers that should not fragment grouping or the join:
# "(t)" / "(tmp)" (tentative/temporary), "(1)", and group-training date stamps.
_LOC_SUFFIX_RE = re.compile(r"[（(].*?[)）]")


def _normalize_location_name(value: Any) -> Optional[str]:
    """Normalize a class ``Location`` for joining + grouping.

    Drops parenthetical qualifiers and collapses whitespace so "San Jose (t)"
    and "San Jose" group together. Returns None when nothing usable remains.
    """
    if _is_blank(value):
        return None
    cleaned = _LOC_SUFFIX_RE.sub("", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–·,")
    return cleaned or None


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "" or s.lower() in ("na", "n/a", "none", "unknown"):
        return None
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return None


def _parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "" or s.lower() in ("na", "n/a", "none", "unknown"):
        return None
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d",
    # Enrollware exports a "Start Date / Time" like "2/1/26 8:00".
    "%m/%d/%y %H:%M", "%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d %b %Y",
)


def _parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("na", "n/a", "none", "unknown"):
        return None
    # ISO first (also handles date-only via fromisoformat on 3.11+).
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").split("+")[0])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _status_to_cancelled(status: Optional[str]) -> Optional[bool]:
    if not status:
        return None
    low = status.lower()
    if any(tok in low for tok in ("cancel", "void", "deleted")):
        return True
    if any(tok in low for tok in ("complete", "held", "active", "scheduled",
                                  "confirmed", "open")):
        return False
    return None


# --------------------------------------------------------------------------- #
# Row -> record
# --------------------------------------------------------------------------- #

def _row_to_record(row: Dict[str, Any], hmap: Dict[str, str]
                   ) -> Optional[EnrollwareClassRecord]:
    def cell(field: str) -> Any:
        col = hmap.get(field)
        return row.get(col) if col else None

    class_name = str(cell("class_name") or "").strip()
    if not class_name:
        return None  # a row with no class name is unusable

    course_type = classify_course_type(class_name)
    dt = _parse_date(cell("date"))
    date_iso: Optional[str] = None
    day_part: Optional[str] = None
    month: Optional[str] = None
    if dt is not None:
        date_iso = dt.strftime("%Y-%m-%d")
        month = dt.strftime("%Y-%m")
        day_part = "weekend" if dt.weekday() >= 5 else "weekday"

    status = (str(cell("status")).strip() if cell("status") is not None else None) or None

    return EnrollwareClassRecord(
        class_name=class_name,
        course_type=course_type,
        course_type_label=COURSE_TYPE_LABELS.get(course_type, course_type),
        date=date_iso,
        day_part=day_part,
        month=month,
        enrolled=_parse_int(cell("enrolled")),
        capacity=_parse_int(cell("capacity")),
        price=_parse_price(cell("price")),
        city=(str(cell("city")).strip() or None) if cell("city") is not None else None,
        location=_normalize_location_name(cell("location")),
        status=status,
        cancelled=_status_to_cancelled(status),
    )


# --------------------------------------------------------------------------- #
# File readers
# --------------------------------------------------------------------------- #

def _read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _read_excel_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        import pandas as pd  # local import; pandas is a project dependency
    except ImportError:
        logger.warning(
            f"enrollware: pandas unavailable, cannot read Excel file {path}"
        )
        return []
    try:
        frame = pd.read_excel(path, dtype=object)
    except Exception as exc:  # openpyxl missing, corrupt file, etc.
        logger.warning(f"enrollware: failed to read Excel {path}: {exc}")
        return []
    frame = frame.where(frame.notna(), None)
    return frame.to_dict(orient="records")


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return _read_excel_rows(path)
    return _read_csv_rows(path)


# --------------------------------------------------------------------------- #
# Locations join — "Location = Abbreviation" -> real city / state
# --------------------------------------------------------------------------- #

# Match the "..., City, ST 12345" or "..., City, ST, 12345" tail of an address.
_CITY_STATE_RE = re.compile(
    r",\s*([A-Za-z .'\-]+?)\s*,\s*([A-Z]{2})\b[,\s]*(\d{5})"
)


def _abbr_key(value: Any) -> str:
    """Normalize a location abbreviation for joining (drop parentheticals)."""
    s = re.sub(r"[（(].*?[)）]", "", str(value or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def _parse_city_state(name: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull (city, state, zip) out of a Locations ``Name`` like
    'San Jose(1631 N First Street, Suite 200, San Jose, CA 95112)'."""
    text = str(name or "")
    m = _CITY_STATE_RE.search(text)
    if m:
        city = re.sub(r"\s+", " ", m.group(1)).strip().title()
        return city, m.group(2).upper(), m.group(3)
    # Fallback: leading token before the address parenthesis.
    lead = re.split(r"[（(]", text, maxsplit=1)[0].strip()
    return (lead or None), None, None


@dataclass
class EnrollwareDataQuality:
    """Per-load data-quality report. Surfaced in logs and the HTML report so the
    files are *cleaned and reported*, never silently rejected."""
    # Classes file
    classes_total_rows: int = 0
    classes_loaded: int = 0
    classes_blank_ignored: int = 0
    missing_location: int = 0
    missing_start_date: int = 0
    missing_end_date: int = 0
    missing_hours: int = 0
    unmatched_locations: int = 0
    ambiguous_location_rows: int = 0
    capacity_overfilled: int = 0
    zero_seats_with_students: int = 0
    missing_zip: int = 0   # class rows whose joined location resolved but had no parseable ZIP
    zero_student_rows: int = 0
    held_classes: int = 0
    # Locations file
    locations_total_rows: int = 0
    locations_loaded: int = 0
    locations_blank_ignored: int = 0
    locations_missing_abbreviation: int = 0
    duplicate_abbreviations: Dict[str, int] = field(default_factory=dict)
    ambiguous_abbreviations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def log_summary(self) -> None:
        logger.info(
            "enrollware data-quality: "
            f"classes loaded={self.classes_loaded} "
            f"(blank ignored={self.classes_blank_ignored} of "
            f"{self.classes_total_rows}); held={self.held_classes}; "
            f"zero-student={self.zero_student_rows}; "
            f"missing location={self.missing_location}, end-date="
            f"{self.missing_end_date}, hours={self.missing_hours}; "
            f"unmatched locations={self.unmatched_locations}, ambiguous rows="
            f"{self.ambiguous_location_rows}; capacity overfilled="
            f"{self.capacity_overfilled}, zero-seats-with-students="
            f"{self.zero_seats_with_students}"
        )
        logger.info(
            "enrollware data-quality: "
            f"locations loaded={self.locations_loaded} "
            f"(blank ignored={self.locations_blank_ignored} of "
            f"{self.locations_total_rows}); missing abbreviation="
            f"{self.locations_missing_abbreviation}; duplicate abbreviations="
            f"{len(self.duplicate_abbreviations)}; ambiguous abbreviations="
            f"{len(self.ambiguous_abbreviations)}"
        )


def _load_locations_detailed(
    path: Optional[Path], dq: EnrollwareDataQuality,
) -> tuple[Dict[str, Dict[str, Optional[str]]], set]:
    """Build the abbreviation -> {city, state, name} map + the set of *ambiguous*
    abbreviations (same code resolving to multiple distinct cities/states).

    Abbreviations are NOT assumed unique: the export has e.g. "Group Training"
    11x and "Plano"/"Troy" 2x. When a code maps to one city we use it; when it
    maps to several different ones we mark it ambiguous and decline to guess the
    city for those classes (better blank than wrong). Counts feed ``dq``.
    """
    target: Optional[Path] = None
    if path is not None:
        target = Path(path)
        if not target.exists():
            logger.warning(f"enrollware: locations file not found: {target}")
            return {}, set()
    else:
        target = next((p for p in LOCATIONS_FILES if p.exists()), None)
    if target is None:
        return {}, set()

    try:
        rows = _read_rows(target)
    except Exception as exc:
        logger.warning(f"enrollware: failed to read locations {target}: {exc}")
        return {}, set()
    if not rows:
        return {}, set()

    dq.locations_total_rows = len(rows)
    headers = {_norm(h): h for h in rows[0].keys()}
    abbr_col = headers.get("abbreviation") or headers.get("abbr") or headers.get("location")
    name_col = headers.get("name") or headers.get("location name") or headers.get("address")
    if not abbr_col or not name_col:
        logger.warning(
            f"enrollware: locations file {target.name} missing "
            f"abbreviation/name columns; skipping join."
        )
        return {}, set()

    # First pass: collect every resolution per abbreviation key so we can detect
    # duplicates and genuine ambiguity (different cities under one code).
    resolutions: Dict[str, List[Dict[str, Optional[str]]]] = {}
    abbr_display: Dict[str, str] = {}
    for row in rows:
        raw_abbr = row.get(abbr_col)
        key = _abbr_key(raw_abbr)
        if not key:
            # Distinguish a fully blank row from a row that has a name but no
            # abbreviation (the latter is a real, reportable data problem).
            if _is_blank(row.get(name_col)):
                dq.locations_blank_ignored += 1
            else:
                dq.locations_missing_abbreviation += 1
            continue
        city, state, zip_code = _parse_city_state(row.get(name_col))
        resolutions.setdefault(key, []).append(
            {"city": city, "state": state, "zip": zip_code,
             "name": str(row.get(name_col) or "").strip()}
        )
        abbr_display.setdefault(key, re.sub(r"\s+", " ", str(raw_abbr or "")).strip())

    out: Dict[str, Dict[str, Optional[str]]] = {}
    ambiguous: set = set()
    for key, hits in resolutions.items():
        if len(hits) > 1:
            dq.duplicate_abbreviations[abbr_display.get(key, key)] = len(hits)
            distinct = {(h["city"], h["state"]) for h in hits
                        if h["city"] or h["state"]}
            if len(distinct) > 1:
                ambiguous.add(key)
                dq.ambiguous_abbreviations.append(abbr_display.get(key, key))
        out[key] = hits[0]  # first occurrence is the representative when usable
        distinct_zips = {h["zip"] for h in hits if h["zip"]}
        if len(distinct_zips) > 1:
            out[key] = dict(hits[0], zip=None)  # several ZIPs: never guess one

    dq.locations_loaded = len(out)
    logger.info(
        f"enrollware: loaded {len(out)} location mapping(s) from {target.name} "
        f"({len(dq.duplicate_abbreviations)} duplicate, {len(ambiguous)} "
        f"ambiguous abbreviations)"
    )
    return out, ambiguous


def load_locations(path: Optional[Path] = None) -> Dict[str, Dict[str, Optional[str]]]:
    """Back-compat: return just the {abbr_key: {city, state, name}} mapping."""
    mapping, _ = _load_locations_detailed(path, EnrollwareDataQuality())
    return mapping


def load_enrollware(
    path: Optional[Path] = None,
    locations_path: Optional[Path] = None,
) -> tuple[List[EnrollwareClassRecord], EnrollwareDataQuality]:
    """Load + normalize Enrollware class records *and* a data-quality report.

    Cleans the files rather than rejecting them: blank formatted rows are
    ignored, location names are normalized before the join, ambiguous
    abbreviations are not force-resolved, missing fields and capacity anomalies
    are counted (never crash). The returned :class:`EnrollwareDataQuality`
    captures everything for the logs and the report.
    """
    dq = EnrollwareDataQuality()
    target: Optional[Path] = None
    if path is not None:
        target = Path(path)
        if not target.exists():
            logger.warning(f"enrollware: file not found: {target}")
            return [], dq
    else:
        target = next((p for p in ENROLLWARE_FILES if p.exists()), None)
    if target is None:
        return [], dq

    try:
        rows = _read_rows(target)
    except Exception as exc:
        logger.warning(f"enrollware: failed to read {target}: {exc}")
        return [], dq
    if not rows:
        return [], dq

    dq.classes_total_rows = len(rows)
    headers = list(rows[0].keys())
    hmap = _build_header_map(headers)
    if "class_name" not in hmap:
        logger.warning(
            f"enrollware: no class-name column detected in {target} "
            f"(headers: {headers}); skipping."
        )
        return [], dq

    locations, ambiguous = _load_locations_detailed(locations_path, dq)
    end_col = hmap.get("end_date")
    hours_col = hmap.get("hours")

    records: List[EnrollwareClassRecord] = []
    for row in rows:
        rec = _row_to_record(row, hmap)
        if rec is None:
            dq.classes_blank_ignored += 1  # blank/formatted-only row
            continue

        # Missing-field accounting (safe — never blocks the row).
        if not rec.location:
            dq.missing_location += 1
        if not rec.date:
            dq.missing_start_date += 1
        if end_col is not None and _is_blank(row.get(end_col)):
            dq.missing_end_date += 1
        if hours_col is not None and _is_blank(row.get(hours_col)):
            dq.missing_hours += 1

        # Locations join — normalized Location is the abbreviation key. Skip
        # ambiguous codes (don't guess a city); count truly unmatched ones.
        if locations and rec.location and not rec.city:
            key = _abbr_key(rec.location)
            hit = locations.get(key)
            if hit and key not in ambiguous:
                rec.city = hit.get("city") or rec.city
                rec.state = hit.get("state") or rec.state
                rec.zip = hit.get("zip") or rec.zip
                if rec.zip is None:
                    dq.missing_zip += 1
            elif key in ambiguous:
                dq.ambiguous_location_rows += 1
            else:
                dq.unmatched_locations += 1
        records.append(rec)

    # Capacity anomalies + zero-student accounting (flagged, never fatal).
    for r in records:
        if (r.enrolled is not None and r.capacity is not None
                and r.capacity > 0 and r.enrolled > r.capacity):
            dq.capacity_overfilled += 1
        if r.capacity == 0 and (r.enrolled or 0) > 0:
            dq.zero_seats_with_students += 1
        if (r.enrolled or 0) == 0:
            dq.zero_student_rows += 1

    dq.classes_loaded = len(records)
    dq.held_classes = len(held_classes(records))
    resolved = sum(1 for r in records if r.city)
    logger.info(
        f"enrollware: loaded {len(records)} class record(s) from {target.name}"
        + (f"; resolved city for {resolved} via locations join" if locations else "")
    )
    dq.log_summary()
    return records, dq


def load_records(
    path: Optional[Path] = None,
    locations_path: Optional[Path] = None,
) -> List[EnrollwareClassRecord]:
    """Back-compat wrapper: return just the records (see :func:`load_enrollware`)."""
    records, _ = load_enrollware(path, locations_path)
    return records
