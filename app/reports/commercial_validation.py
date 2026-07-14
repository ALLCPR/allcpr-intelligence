"""
Manual commercial-validation layer (CSV → per-ZIP summary).

A ZIP can score well on demand yet be impractical to lease: rent too high, no
parking, no classroom-sized space, nothing available, bad access, or no broker.
This module reads a hand-maintained CSV of validated commercial spaces and
summarizes it per ZIP so the dashboard can show real-estate reality next to the
modeled/historical demand — and so the recommendation can be upgraded only when
a usable space actually exists.

It is deliberately manual: no LoopNet/Crexi scraping. A missing or malformed
file never crashes — it just yields an empty summary.

CSV columns (header-driven, extras ignored):
    zip,address,property_name,sqft,monthly_rent,parking,available,
    classroom_fit,source_url,broker_contact,notes,updated_at
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import MANUAL_DIR
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

COMMERCIAL_VALIDATION_FILE = MANUAL_DIR / "commercial_validation.csv"

_POSITIVE_PARKING = {"yes", "y", "true", "good", "available", "ample", "ok"}
_POSITIVE_AVAILABLE = {"yes", "y", "true", "available", "1"}
_GOOD_FIT = {"good", "great", "excellent"}
_POSSIBLE_FIT = {"possible", "ok", "fair", "maybe", "limited"}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse_money(value: Any) -> Optional[float]:
    """Parse a rent string like '$3,200' / '3200' → float; None if not numeric."""
    text = _clean(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    return out if out > 0 else None


def load_commercial_validation(
    path: Path = COMMERCIAL_VALIDATION_FILE,
) -> Dict[str, List[Dict[str, Any]]]:
    """Load the CSV grouped by 5-digit ZIP. Missing file → ``{}``.

    Bad rows (no parseable ZIP) are skipped with a warning; the rest load.
    """
    p = Path(path)
    if not p.exists():
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for i, raw in enumerate(reader, start=2):
                zip_code = _clean(raw.get("zip")).zfill(5)
                if len(zip_code) != 5 or not zip_code.isdigit():
                    logger.warning(
                        f"commercial_validation.csv row {i}: bad/missing ZIP "
                        f"{raw.get('zip')!r}; skipped.")
                    continue
                out.setdefault(zip_code, []).append({
                    "zip": zip_code,
                    "address": _clean(raw.get("address")),
                    "property_name": _clean(raw.get("property_name")),
                    "sqft": _clean(raw.get("sqft")),
                    "monthly_rent": _parse_money(raw.get("monthly_rent")),
                    "parking": _clean(raw.get("parking")),
                    "available": _clean(raw.get("available")),
                    "classroom_fit": _clean(raw.get("classroom_fit")),
                    "source_url": _clean(raw.get("source_url")),
                    "broker_contact": _clean(raw.get("broker_contact")),
                    "notes": _clean(raw.get("notes")),
                    "updated_at": _clean(raw.get("updated_at")),
                })
    except (OSError, csv.Error) as exc:
        logger.warning(f"commercial_validation.csv unreadable: {exc}")
        return {}
    return out


def _is_available(row: Dict[str, Any]) -> bool:
    return _clean(row.get("available")).lower() in _POSITIVE_AVAILABLE


def _parking_summary(rows: List[Dict[str, Any]]) -> str:
    vals = [_clean(r.get("parking")).lower() for r in rows if _clean(r.get("parking"))]
    if not vals:
        return "Unknown"
    pos = sum(1 for v in vals if v in _POSITIVE_PARKING)
    if pos == len(vals):
        return "Yes"
    if pos == 0:
        return "No"
    return "Mixed"


def _classroom_fit_summary(rows: List[Dict[str, Any]]) -> str:
    fits = [_clean(r.get("classroom_fit")).lower() for r in rows
            if _clean(r.get("classroom_fit"))]
    if not fits:
        return "Unknown"
    if any(f in _GOOD_FIT for f in fits):
        return "Good"
    if any(f in _POSSIBLE_FIT for f in fits):
        return "Possible"
    return "Limited"


def summarize_commercial_validation(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce a ZIP's commercial rows to a compact, display-ready summary.

    Also exposes ``commercial_ready`` — true only when at least one available
    space has usable parking and a usable classroom fit — which the decision
    logic uses to (cautiously) upgrade a recommendation.
    """
    if not rows:
        return {"commercial_validated": False}

    rents = [r["monthly_rent"] for r in rows if r.get("monthly_rent") is not None]
    available = [r for r in rows if _is_available(r)]
    parking = _parking_summary(rows)
    classroom = _classroom_fit_summary(rows)
    sources = [r["source_url"] for r in rows if r.get("source_url")]
    notes = [r["notes"] for r in rows if r.get("notes")]
    updated = sorted([r["updated_at"] for r in rows if r.get("updated_at")])

    commercial_ready = bool(
        available and parking in ("Yes", "Mixed")
        and classroom in ("Good", "Possible")
    )

    return {
        "commercial_validated": True,
        "commercial_space_count": len(rows),
        "available_space_count": len(available),
        "rent_min": min(rents) if rents else None,
        "rent_max": max(rents) if rents else None,
        "rent_avg": round(sum(rents) / len(rents), 0) if rents else None,
        "parking_summary": parking,
        "classroom_fit_summary": classroom,
        "commercial_notes": notes,
        "commercial_sources": sources,
        "commercial_updated_at": updated[-1] if updated else None,
        "commercial_ready": commercial_ready,
        "spaces": rows,
    }


def load_commercial_summaries(
    path: Path = COMMERCIAL_VALIDATION_FILE,
) -> Dict[str, Dict[str, Any]]:
    """Convenience: ``{zip: summary}`` for every ZIP in the CSV. Empty if none."""
    grouped = load_commercial_validation(path)
    return {z: summarize_commercial_validation(rows) for z, rows in grouped.items()}
