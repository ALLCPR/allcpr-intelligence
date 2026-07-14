#!/usr/bin/env python3
"""
Convert raw Enrollware / competitor / student exports into the ops-layer
manual imports the Operating Readiness panel already consumes.

This is the bridge that turns the panel from "signal only" into "who to
contact": it reads the hand-exported spreadsheets from Enrollware (the real
ALLCPR instructor roster and location list), the Red Cross / competitor course
scrape, and the 6-month student export, and writes header-driven CSVs under
``data/manual/``:

    data/manual/allcpr_instructors.csv   real named instructor candidates
    data/manual/allcpr_locations.csv     real ALLCPR class venues
    data/manual/competitor_classes.csv   competitor class supply + pricing by ZIP
    data/manual/local_demand.csv         real student demand by home ZIP

All four outputs are gitignored (they carry internal + third-party PII); only
the synthetic ``*.example.csv`` files are tracked. Nothing here is marked
"verified" — roster rows stay ``NEEDS_VERIFICATION`` until a human confirms a
credential, per the ops-layer honesty rule.

The transform functions (``instructor_record``, ``location_record``,
``parse_location_name``, ``aggregate_competitor_classes``,
``aggregate_local_demand``) are pure dict-in/dict-out so they can be unit
tested without any spreadsheet.

Usage:
    python3 scripts/import_enrollware_ops_data.py \
        --source "Enrollware自动化数据导出" [--dry-run]

``--source`` defaults to the ``Enrollware自动化数据导出`` folder at the repo
root; individual files can be overridden with ``--instructors``,
``--locations``, ``--competitor``, ``--students``. Missing inputs are skipped
with a warning, never a crash.
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED  # noqa: E402

DEFAULT_SOURCE_DIR = REPO_ROOT / "Enrollware自动化数据导出"
MANUAL_DIR = REPO_ROOT / "data" / "manual"

INSTRUCTORS_OUT = MANUAL_DIR / "allcpr_instructors.csv"
LOCATIONS_OUT = MANUAL_DIR / "allcpr_locations.csv"
COMPETITOR_OUT = MANUAL_DIR / "competitor_classes.csv"
DEMAND_OUT = MANUAL_DIR / "local_demand.csv"
PERFORMANCE_OUT = MANUAL_DIR / "instructor_performance.csv"

INSTRUCTOR_COLUMNS = [
    "name", "email", "phone", "city", "state", "zip", "courses",
    "certifications", "expiration_dates", "travel_radius_miles",
    "availability", "pay_rate", "reliability_notes", "languages",
    "long_term_interest", "verified",
]
LOCATION_COLUMNS = [
    "location_name", "address", "zip", "active_status", "courses_offered",
    "capacity", "rent_cost", "parking_notes", "room_notes",
    "average_monthly_enrollment",
]
COMPETITOR_COLUMNS = [
    "zip", "course_type", "class_count", "provider_count", "median_price",
    "min_price", "max_price", "providers", "sample_locations",
]
DEMAND_COLUMNS = [
    "zip", "student_count", "class_count", "top_courses", "instructor_count",
    "latest_registration",
]
PERFORMANCE_COLUMNS = [
    "name", "students", "classes", "zips", "students_per_class",
    "aha_bls_students", "arc_bls_students", "arc_cpr_students",
    "teaches_aha", "teaches_arc", "home_city", "home_state", "home_zip",
    "last_taught",
]

# Enrollware "Instructor" cells that are really schedule/status markers, not
# people — must never become a performance row or a sourcing benchmark.
_NON_INSTRUCTOR_PREFIXES = (
    "cancel", "tentative", "hold", "test", "n/a", "tbd", "pending",
    "unassigned", "staff", "(",
)


# --------------------------------------------------------------------------
# Small cell helpers (tolerant of pandas NaN / float ids / stray whitespace)
# --------------------------------------------------------------------------
def cell(value: Any) -> str:
    """Clean a spreadsheet cell to a trimmed string ('' for NaN/None)."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "nat", "none"):
        return ""
    return text


def has_id(value: Any) -> bool:
    """True when an AHA/HSI id cell actually holds an id (not blank/NaN)."""
    text = cell(value)
    if not text:
        return False
    # pandas reads numeric ids as e.g. "10102081798.0" — a lone "0"/"0.0" is
    # not a real id.
    return text not in ("0", "0.0")


def normalize_zip(value: Any) -> str:
    """First 5-digit ZIP found in a cell, zero-padded; '' when none."""
    text = cell(value)
    if not text:
        return ""
    # Strip a trailing ".0" from floaty ZIPs, then find 5 consecutive digits.
    text = re.sub(r"\.0$", "", text)
    match = re.search(r"\d{5}", text)
    if match:
        return match.group(0)
    digits = re.sub(r"\D", "", text)
    return digits.zfill(5) if digits else ""


def split_multiline(value: Any) -> List[str]:
    """Split a newline/comma/semicolon-delimited cell into cleaned items."""
    text = cell(value)
    if not text:
        return []
    parts = re.split(r"[\n;,]+", text)
    return [p.strip() for p in parts if p.strip()]


def _semicolon(items: Iterable[str]) -> str:
    """Join items with '; ' — the delimiter the ops importer splits on."""
    seen: List[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
    return "; ".join(seen)


# --------------------------------------------------------------------------
# Instructor roster
# --------------------------------------------------------------------------
_SALARY_RE = re.compile(r"#?\s*Salary\s*[:=]\s*\$?\s*([\d,.]+)", re.IGNORECASE)


def parse_salary(notes: Any) -> str:
    """Pull the '#Salary:45' figure out of the Enrollware Notes blob."""
    text = cell(notes)
    if not text:
        return ""
    match = _SALARY_RE.search(text)
    if not match:
        return ""
    amount = match.group(1).replace(",", "").rstrip(".")
    return f"${amount}" if amount else ""


def derive_courses(aha_id: Any, hsi_id: Any,
                   cert_text: Any) -> List[str]:
    """Best-effort teachable courses from instructor-id presence + cert text.

    Honest and conservative: an AHA instructor id implies AHA BLS capability;
    an HSI/ASHI id implies ARC-style CPR/BLS capability; explicit cert wording
    overrides. Never invents a credential — this only shapes which course
    columns a *candidate* is offered for; verification still happens later.
    """
    courses: List[str] = []
    haystack = cell(cert_text).lower()
    if has_id(aha_id) or "aha" in haystack or "american heart" in haystack:
        courses.append(AHA_BLS)
    if "arc" in haystack or "red cross" in haystack:
        courses.append(ARC_BLS)
        courses.append(ARC_CPR_FA_AED)
    if has_id(hsi_id) and ARC_BLS not in courses:
        # HSI / ASHI instructors map onto ALLCPR's ARC CPR/BLS product line.
        courses.append(ARC_BLS)
        courses.append(ARC_CPR_FA_AED)
    # De-dup, preserve order.
    out: List[str] = []
    for c in courses:
        if c not in out:
            out.append(c)
    return out


def cert_signals(aha_id: Any, hsi_id: Any, cert_text: Any) -> List[str]:
    """Human-readable credential *signals* (never the raw id numbers)."""
    signals = split_multiline(cert_text)
    if has_id(aha_id):
        signals.append("AHA instructor ID on file")
    if has_id(hsi_id):
        signals.append("HSI/ASHI instructor ID on file")
    out: List[str] = []
    for s in signals:
        if s and s not in out:
            out.append(s)
    return out


def instructor_record(raw: Dict[str, Any],
                      missing_cert_emails: Optional[set] = None
                      ) -> Optional[Dict[str, str]]:
    """One Enrollware instructor row → a manual-import CSV dict.

    Returns ``None`` for inactive rows or rows with no name (skipped). The
    output matches ``load_instructors_import`` columns exactly.
    """
    if cell(raw.get("Active")).lower() not in ("yes", "y", "true", "1", "active"):
        return None
    first = cell(raw.get("First Name"))
    last = cell(raw.get("Last Name"))
    name = " ".join(p for p in (first, last) if p).strip()
    if not name:
        return None

    aha_id, hsi_id = raw.get("AHA ID"), raw.get("HSI ID")
    cert_text = raw.get("Certifications")
    courses = derive_courses(aha_id, hsi_id, cert_text)
    signals = cert_signals(aha_id, hsi_id, cert_text)

    email = cell(raw.get("Email"))
    reliability_bits: List[str] = ["Existing ALLCPR instructor (roster import)"]
    if missing_cert_emails and email.lower() in missing_cert_emails:
        reliability_bits.append(
            "On instructor cert-renewal reminder list — reverify credentials")

    return {
        "name": name,
        "email": email,
        "phone": cell(raw.get("Phone")),
        "city": cell(raw.get("City")),
        "state": cell(raw.get("State")),
        "zip": normalize_zip(raw.get("Zip")),
        "courses": _semicolon(courses),
        "certifications": _semicolon(signals),
        "expiration_dates": _semicolon(
            split_multiline(raw.get("Certifications - Expiration Dates"))),
        "travel_radius_miles": "",
        "availability": "",
        "pay_rate": parse_salary(raw.get("Notes")),
        "reliability_notes": " — ".join(reliability_bits),
        "languages": "",
        "long_term_interest": "UNKNOWN",
        # Honesty rule: roster rows are claims, never pre-verified.
        "verified": "no",
    }


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------
def parse_location_name(name: Any) -> Tuple[str, str, str]:
    """Split 'Albany (124 Washington Ave, Albany, NY 12203)' →
    (label, address, zip). Falls back gracefully when there is no paren blob.
    """
    text = cell(name)
    if not text:
        return "", "", ""
    match = re.match(r"^(.*?)\s*\((.*)\)\s*$", text)
    if not match:
        return text, "", ""
    label = match.group(1).strip() or text
    address = match.group(2).strip()
    zip_code = normalize_zip(address)
    return label, address, zip_code


def location_record(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """One Enrollware location row → a manual-import CSV dict.

    Enrollware locations are venues ALLCPR actively schedules classes at, so
    they import as ``active`` (a room ALLCPR already uses). Rows with no usable
    ZIP still import — they only surface for a matching or same ZIP.
    """
    label, address, zip_code = parse_location_name(raw.get("Name"))
    name = label or cell(raw.get("Abbreviation"))
    if not name:
        return None
    directions = cell(raw.get("Directions"))
    return {
        "location_name": name,
        "address": address,
        "zip": zip_code,
        "active_status": "active",
        "courses_offered": "",
        "capacity": "",
        "rent_cost": "",
        "parking_notes": "",
        "room_notes": (directions[:280] if directions else ""),
        "average_monthly_enrollment": "",
    }


# --------------------------------------------------------------------------
# Competitor class supply + pricing (Red Cross / CPS scrape)
# --------------------------------------------------------------------------
def _price(value: Any) -> Optional[float]:
    text = cell(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def aggregate_competitor_classes(rows: Iterable[Dict[str, Any]]
                                 ) -> List[Dict[str, str]]:
    """Group competitor course rows into per-(ZIP, course_type) supply/price.

    Uses the class's venue ZIP so the numbers describe *where the class is
    taught*, and reports median/min/max price, provider spread, and a couple
    of sample venues staff can eyeball.
    """
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        zip_code = normalize_zip(row.get("venue_zipcode") or row.get("zipcode"))
        if not zip_code:
            continue
        course = (cell(row.get("course_type")) or "OTHER").upper()
        key = (zip_code, course)
        b = buckets.setdefault(key, {
            "prices": [], "providers": set(), "locations": []})
        price = _price(row.get("price"))
        if price is not None:
            b["prices"].append(price)
        provider = cell(row.get("provider"))
        if provider:
            b["providers"].add(provider)
        loc = cell(row.get("location"))
        if loc and len(b["locations"]) < 3 and loc not in b["locations"]:
            b["locations"].append(loc)
        b["count"] = b.get("count", 0) + 1

    out: List[Dict[str, str]] = []
    for (zip_code, course), b in sorted(buckets.items()):
        prices = b["prices"]
        out.append({
            "zip": zip_code,
            "course_type": course,
            "class_count": str(b["count"]),
            "provider_count": str(len(b["providers"])),
            "median_price": (f"{statistics.median(prices):.0f}"
                             if prices else ""),
            "min_price": (f"{min(prices):.0f}" if prices else ""),
            "max_price": (f"{max(prices):.0f}" if prices else ""),
            "providers": _semicolon(sorted(b["providers"])[:6]),
            "sample_locations": _semicolon(b["locations"]),
        })
    return out


# --------------------------------------------------------------------------
# Local demand (6-month student export, by home ZIP)
# --------------------------------------------------------------------------
def _date_sort_key(value: Any) -> Tuple[int, int, int]:
    """Sortable (year, month, day) from an 'M/D/YY', 'M/D/YYYY' or ISO date.

    Returns ``(0, 0, 0)`` when unparseable so a real date always wins.
    """
    text = cell(value).split()[0] if cell(value) else ""
    if not text:
        return (0, 0, 0)
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    mdy = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if mdy:
        year = int(mdy.group(3))
        if year < 100:
            year += 2000
        return (year, int(mdy.group(1)), int(mdy.group(2)))
    return (0, 0, 0)


def _date_display(value: Any) -> str:
    """Just the date portion of a registration timestamp cell."""
    text = cell(value)
    return text.split()[0] if text else ""


def aggregate_local_demand(rows: Iterable[Dict[str, Any]]
                           ) -> List[Dict[str, str]]:
    """Group real students by home (mailing) ZIP into a demand summary."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        zip_code = normalize_zip(
            row.get("Mailing Zip") or row.get("Billing Zip"))
        if not zip_code:
            continue
        b = buckets.setdefault(zip_code, {
            "students": 0, "courses": {}, "classes": set(),
            "instructors": set(), "latest_key": (0, 0, 0), "latest": ""})
        b["students"] += 1
        course = cell(row.get("Course")) or cell(row.get("Discipline"))
        if course:
            b["courses"][course] = b["courses"].get(course, 0) + 1
        class_id = cell(row.get("Class ID"))
        if class_id:
            b["classes"].add(class_id)
        instr = cell(row.get("Instructor"))
        if instr:
            b["instructors"].add(instr)
        reg = row.get("Reg. Date")
        key = _date_sort_key(reg)
        if key > b["latest_key"]:
            b["latest_key"] = key
            b["latest"] = _date_display(reg)

    out: List[Dict[str, str]] = []
    for zip_code, b in sorted(buckets.items(),
                              key=lambda kv: kv[1]["students"], reverse=True):
        top = sorted(b["courses"].items(), key=lambda kv: kv[1], reverse=True)
        top_courses = _semicolon(f"{name} ({n})" for name, n in top[:3])
        out.append({
            "zip": zip_code,
            "student_count": str(b["students"]),
            "class_count": str(len(b["classes"])),
            "top_courses": top_courses,
            "instructor_count": str(len(b["instructors"])),
            "latest_registration": b["latest"],
        })
    return out


# --------------------------------------------------------------------------
# Instructor performance (6-month student export → per-instructor track record)
# --------------------------------------------------------------------------
def is_real_instructor(name: Any) -> bool:
    """False for blank/NaN or schedule-status markers ('Cancel (weather)')."""
    text = cell(name).lower()
    if not text or text in ("nan", "none"):
        return False
    return not text.startswith(_NON_INSTRUCTOR_PREFIXES)


def discipline_to_course(discipline: Any) -> Optional[str]:
    """Map an Enrollware discipline/course label to an ops course type.

    Handles both short discipline codes ("ARC BLS", "ARC CPR", "BLS") and the
    long course names ("Red Cross Basic Life Support-BL R.25", "AHA© BLS
    Provider Course", "Red Cross Adult/Pediatric First Aid/CPR/AED"). Red Cross
    (ARC) is checked first; a bare BLS/AHA label is the AHA BLS Provider line.
    """
    d = cell(discipline).upper()
    if not d:
        return None
    is_arc = "ARC" in d or "RED CROSS" in d
    has_bls = "BLS" in d or "BASIC LIFE SUPPORT" in d
    has_cpr = "CPR" in d or "FIRST AID" in d or "AED" in d
    if is_arc:
        if has_bls:
            return ARC_BLS
        if has_cpr:
            return ARC_CPR_FA_AED
        return ARC_CPR_FA_AED   # generic Red Cross course → CPR/FA/AED line
    if "AHA" in d or "AMERICAN HEART" in d or has_bls:
        return AHA_BLS          # AHA BLS Provider line (incl. bare "BLS")
    if has_cpr:
        return ARC_CPR_FA_AED
    return None


def aggregate_instructor_performance(
        rows: Iterable[Dict[str, Any]],
        roster_lookup: Optional[Dict[str, Dict[str, str]]] = None
        ) -> List[Dict[str, str]]:
    """Per-instructor 6-month track record, joined to roster home location.

    One row per real instructor: volume (students/classes), reach (distinct
    student ZIPs), per-course-type student counts (which double as the honest
    "what they are credentialed and authorized to teach" signal), and last
    taught date. Sorted by students desc.
    """
    roster_lookup = roster_lookup or {}
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = cell(row.get("Instructor"))
        if not is_real_instructor(name):
            continue
        b = buckets.setdefault(name, {
            "students": 0, "classes": set(), "zips": set(),
            AHA_BLS: 0, ARC_BLS: 0, ARC_CPR_FA_AED: 0,
            "last_key": (0, 0, 0), "last": ""})
        b["students"] += 1
        class_id = cell(row.get("Class ID"))
        if class_id:
            b["classes"].add(class_id)
        zip_code = normalize_zip(row.get("Mailing Zip"))
        if zip_code:
            b["zips"].add(zip_code)
        course = discipline_to_course(
            row.get("Discipline") or row.get("Course"))
        if course:
            b[course] += 1
        taught = row.get("Course Date") or row.get("Reg. Date")
        key = _date_sort_key(taught)
        if key > b["last_key"]:
            b["last_key"] = key
            b["last"] = _date_display(taught)

    out: List[Dict[str, str]] = []
    for name, b in sorted(buckets.items(),
                          key=lambda kv: kv[1]["students"], reverse=True):
        classes = len(b["classes"]) or 0
        home = roster_lookup.get(name.lower(), {})
        out.append({
            "name": name,
            "students": str(b["students"]),
            "classes": str(classes),
            "zips": str(len(b["zips"])),
            "students_per_class": (f"{b['students'] / classes:.1f}"
                                   if classes else ""),
            "aha_bls_students": str(b[AHA_BLS]),
            "arc_bls_students": str(b[ARC_BLS]),
            "arc_cpr_students": str(b[ARC_CPR_FA_AED]),
            "teaches_aha": "yes" if b[AHA_BLS] > 0 else "no",
            "teaches_arc": "yes" if (b[ARC_BLS] + b[ARC_CPR_FA_AED]) > 0 else "no",
            "home_city": home.get("city", ""),
            "home_state": home.get("state", ""),
            "home_zip": home.get("zip", ""),
            "last_taught": b["last"],
        })
    return out


# --------------------------------------------------------------------------
# Spreadsheet reading + CSV writing
# --------------------------------------------------------------------------
def _read_xlsx(path: Path) -> List[Dict[str, Any]]:
    """Read the first sheet of an xlsx into stripped-header dict rows."""
    try:
        import pandas as pd  # noqa: PLC0415 — heavy import, only when needed
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f"pandas is required to read {path}: {exc}")
    frame = pd.read_excel(path)
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame.to_dict(orient="records")


def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, str]],
               dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would write {len(rows)} rows → {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    print(f"  wrote {len(rows)} rows → {path.relative_to(REPO_ROOT)}")


def _resolve(source_dir: Path, override: Optional[str],
             *candidates: str) -> Optional[Path]:
    if override:
        p = Path(override)
        return p if p.exists() else None
    for name in candidates:
        p = source_dir / name
        if p.exists():
            return p
    return None


def _missing_cert_emails(students_path: Optional[Path],
                         renew_path: Optional[Path]) -> set:
    """Emails on the instructor cert-renewal reminder list (best effort)."""
    if not renew_path or not renew_path.exists():
        return set()
    try:
        rows = _read_xlsx(renew_path)
    except SystemExit:
        return set()
    return {cell(r.get("Email")).lower()
            for r in rows if cell(r.get("Email"))}


def convert(source_dir: Path, *, instructors: Optional[str] = None,
            locations: Optional[str] = None, competitor: Optional[str] = None,
            students: Optional[str] = None, renew: Optional[str] = None,
            dry_run: bool = False) -> Dict[str, int]:
    """Run every available conversion; return a per-output row-count summary."""
    summary: Dict[str, int] = {}

    renew_path = _resolve(
        source_dir, renew,
        "Reports/Instructor Certificate Renew Reminder List.xlsx")
    missing_emails = _missing_cert_emails(None, renew_path)

    roster_lookup: Dict[str, Dict[str, str]] = {}
    inst_path = _resolve(source_dir, instructors,
                         "Enrollware Data - Instructors.xlsx")
    if inst_path:
        raw_rows = _read_xlsx(inst_path)
        rows = [r for r in (instructor_record(rr, missing_emails)
                            for rr in raw_rows) if r]
        # name → home location, for the performance join below.
        for rec in rows:
            roster_lookup[rec["name"].lower()] = {
                "city": rec["city"], "state": rec["state"], "zip": rec["zip"]}
        _write_csv(INSTRUCTORS_OUT, INSTRUCTOR_COLUMNS, rows, dry_run)
        summary["instructors"] = len(rows)
    else:
        print("  ! instructor roster not found — skipped")

    loc_path = _resolve(source_dir, locations,
                        "Enrollware Data - Locations.xlsx")
    if loc_path:
        rows = [location_record(r) for r in _read_xlsx(loc_path)]
        rows = [r for r in rows if r]
        _write_csv(LOCATIONS_OUT, LOCATION_COLUMNS, rows, dry_run)
        summary["locations"] = len(rows)
    else:
        print("  ! location list not found — skipped")

    comp_path = _resolve(source_dir, competitor,
                         "Red Cross AD Data/Latest CPS Data.xlsx")
    if comp_path:
        rows = aggregate_competitor_classes(_read_xlsx(comp_path))
        _write_csv(COMPETITOR_OUT, COMPETITOR_COLUMNS, rows, dry_run)
        summary["competitor_classes"] = len(rows)
    else:
        print("  ! competitor CPS data not found — skipped")

    stu_path = _resolve(source_dir, students,
                        "Students Export in 6 Months.xlsx",
                        "Student list for the next three weeks.xlsx")
    if stu_path:
        student_rows = _read_xlsx(stu_path)
        demand = aggregate_local_demand(student_rows)
        _write_csv(DEMAND_OUT, DEMAND_COLUMNS, demand, dry_run)
        summary["local_demand"] = len(demand)
        perf = aggregate_instructor_performance(student_rows, roster_lookup)
        _write_csv(PERFORMANCE_OUT, PERFORMANCE_COLUMNS, perf, dry_run)
        summary["instructor_performance"] = len(perf)
    else:
        print("  ! student export not found — skipped")

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE_DIR),
                        help="Directory holding the Enrollware exports")
    parser.add_argument("--instructors", help="Override instructor xlsx path")
    parser.add_argument("--locations", help="Override locations xlsx path")
    parser.add_argument("--competitor", help="Override competitor xlsx path")
    parser.add_argument("--students", help="Override student export xlsx path")
    parser.add_argument("--renew", help="Override cert-renewal list xlsx path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be written without writing")
    args = parser.parse_args(argv)

    source_dir = Path(args.source)
    if not source_dir.exists() and not any(
            (args.instructors, args.locations, args.competitor,
             args.students)):
        print(f"Source directory not found: {source_dir}", file=sys.stderr)
        return 1

    print(f"Converting Enrollware exports from: {source_dir}")
    summary = convert(
        source_dir, instructors=args.instructors, locations=args.locations,
        competitor=args.competitor, students=args.students, renew=args.renew,
        dry_run=args.dry_run)
    print("Done." + ("  (dry-run — nothing written)" if args.dry_run else ""))
    for key, count in summary.items():
        print(f"  {key}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
