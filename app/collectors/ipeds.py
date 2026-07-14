"""
IPEDS education collector → per-ZIP college / nursing-school counts.

Colleges, nursing programs, and allied-health schools are a real BLS-demand
driver (students needing certification). IPEDS publishes institution-level CSVs
(NCES) with a ZIP and institution name; we count per ZIP and flag nursing /
health-program schools by name keyword.

Pure + schema-tolerant (find ZIP / name / enrollment columns by header). Missing
file → ``{}``. Real data: NCES IPEDS "Institutional Characteristics (HD)" CSV,
optionally joined with a 12-month enrollment file. Drop under data/raw/bulk/.

Per-ZIP fields:
    college_count, nursing_school_count, health_program_school_count,
    student_enrollment_count
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

_ZIP_COLS = ("ZIP", "ZIPCODE", "ZIP_CODE")
_NAME_COLS = ("INSTNM", "NAME", "INSTITUTION", "INSTITUTION_NAME")
_ENROLL_COLS = ("EFTOTLT", "DRVEF", "ENROLLMENT", "TOTAL_ENROLLMENT", "STUDENTS")

_NURSING_KEYWORDS = ("nursing", "nurse")
_HEALTH_KEYWORDS = (
    "nursing", "nurse", "health", "medical", "dental", "pharmacy",
    "therapy", "paramedic", "emt", "allied health", "respiratory",
    "radiolog", "surgical tech",
)


def _detect(header: List[str], candidates) -> Optional[int]:
    upper = [h.strip().upper() for h in header]
    for cand in candidates:
        if cand in upper:
            return upper.index(cand)
    return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_ipeds(path: Path) -> List[Dict[str, Any]]:
    """Parse an IPEDS institutions CSV → ``[{zip, name, enrollment}]``."""
    p = Path(path)
    if not p.exists():
        logger.warning(f"IPEDS: file not found, skipping: {p}")
        return []
    out: List[Dict[str, Any]] = []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return []
            i_zip = _detect(header, _ZIP_COLS)
            i_name = _detect(header, _NAME_COLS)
            i_enroll = _detect(header, _ENROLL_COLS)
            if i_zip is None:
                logger.warning("IPEDS: no ZIP column; skipping.")
                return []
            for row in reader:
                z = str(row[i_zip]).strip()[:5].zfill(5) if i_zip < len(row) else ""
                if len(z) != 5 or not z.isdigit():
                    continue
                name = (str(row[i_name]).strip()
                        if i_name is not None and i_name < len(row) else "")
                enroll = (_num(row[i_enroll])
                          if i_enroll is not None and i_enroll < len(row) else None)
                out.append({"zip": z, "name": name, "enrollment": enroll})
    except (OSError, csv.Error) as exc:
        logger.warning(f"IPEDS: failed to read {p}: {exc}")
        return []
    return out


def aggregate_ipeds_by_zip(
    institutions: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate parsed institutions into per-ZIP education counts."""
    out: Dict[str, Dict[str, Any]] = {}
    for inst in institutions:
        z = inst["zip"]
        name = (inst.get("name") or "").lower()
        bucket = out.setdefault(z, {
            "college_count": 0, "nursing_school_count": 0,
            "health_program_school_count": 0, "student_enrollment_count": 0})
        bucket["college_count"] += 1
        if any(k in name for k in _NURSING_KEYWORDS):
            bucket["nursing_school_count"] += 1
        if any(k in name for k in _HEALTH_KEYWORDS):
            bucket["health_program_school_count"] += 1
        if inst.get("enrollment"):
            bucket["student_enrollment_count"] += int(inst["enrollment"])
    return out


def load_ipeds(path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse + aggregate one IPEDS CSV to ``{zip: {...}}``. ``{}`` if missing."""
    return aggregate_ipeds_by_zip(parse_ipeds(path))
