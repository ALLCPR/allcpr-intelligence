"""
Commercial-listing validation layer.

There is **no free commercial-real-estate API** — LoopNet / Crexi / CoStar all
prohibit scraping. So a candidate becomes a confirmed *site* (not just a good
*area*) only when a human has validated a real leasable space. That validation
is supplied via a manual override CSV:

    data/raw/commercial_overrides.csv
    columns: candidate_id, address_match, listing_url, asking_rent,
             square_feet, parking_notes, broker_notes, validation_status

``validation_status`` is one of: ``validated`` | ``pending`` | ``rejected``.
Only ``validated`` rows unlock a real ``site_score``.

Matching is by ``candidate_id`` first (exact), then a loose address/city
substring match so an analyst can paste a street address without knowing the
generated candidate id.

``fetch_live()`` is a pluggable stub returning ``None`` — drop in a paid
listings API later without touching callers. When no override matches, the
candidate stays an *area-level* candidate (proxy), never a confirmed site.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.config import COMMERCIAL_OVERRIDES_FILE
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class CommercialOverride:
    candidate_id: str = ""
    address_match: str = ""
    listing_url: str = ""
    asking_rent: Optional[float] = None        # $/sqft/yr (cited)
    square_feet: Optional[float] = None
    parking_notes: str = ""
    broker_notes: str = ""
    validation_status: str = "pending"          # validated | pending | rejected

    @property
    def is_validated(self) -> bool:
        return self.validation_status.strip().lower() == "validated"

    def to_dict(self) -> Dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "address_match": self.address_match,
            "listing_url": self.listing_url,
            "asking_rent": self.asking_rent,
            "square_feet": self.square_feet,
            "parking_notes": self.parking_notes,
            "broker_notes": self.broker_notes,
            "validation_status": self.validation_status,
        }


def _to_float(value: object) -> Optional[float]:
    try:
        s = str(value).strip().replace("$", "").replace(",", "")
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def _load(path: Path) -> List[CommercialOverride]:
    if not Path(path).exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"commercial_listings: failed to read {path}: {exc}")
        return []
    out: List[CommercialOverride] = []
    for r in rows:
        cid = (r.get("candidate_id") or "").strip()
        addr = (r.get("address_match") or "").strip()
        if not cid and not addr:
            continue
        out.append(CommercialOverride(
            candidate_id=cid,
            address_match=addr,
            listing_url=(r.get("listing_url") or "").strip(),
            asking_rent=_to_float(r.get("asking_rent")),
            square_feet=_to_float(r.get("square_feet")),
            parking_notes=(r.get("parking_notes") or "").strip(),
            broker_notes=(r.get("broker_notes") or "").strip(),
            validation_status=(r.get("validation_status") or "pending").strip(),
        ))
    return out


_CACHE: Optional[List[CommercialOverride]] = None


def _table() -> List[CommercialOverride]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load(COMMERCIAL_OVERRIDES_FILE)
    return _CACHE


def reload() -> None:
    """Force re-read on next lookup (for tests)."""
    global _CACHE
    _CACHE = None


def fetch_live(profile: Dict[str, object]) -> Optional[CommercialOverride]:
    """Pluggable live-listings hook. Returns ``None`` (no free CRE API).

    Swap this out for a paid provider later; callers already treat ``None`` as
    "no live data — fall back to manual override / proxy."
    """
    return None


def _addr_haystack(profile: Dict[str, object]) -> str:
    anchor = profile.get("anchor") or {}
    parts = [
        str(profile.get("candidate_name") or ""),
        str((anchor or {}).get("formatted_address") or ""),
        str((anchor or {}).get("name") or ""),
        str(profile.get("city") or ""),
    ]
    return " | ".join(p for p in parts if p).lower()


def lookup_override(profile: Dict[str, object]) -> Optional[CommercialOverride]:
    """Return the best matching override for a candidate, or ``None``.

    Order: live data (currently always None) → exact candidate_id → loose
    address/city substring match. Never fabricates a listing.
    """
    live = fetch_live(profile)
    if live is not None:
        return live

    table = _table()
    if not table:
        return None

    cid = str(profile.get("candidate_id") or "").strip()
    if cid:
        for ov in table:
            if ov.candidate_id and ov.candidate_id == cid:
                return ov

    haystack = _addr_haystack(profile)
    if haystack:
        for ov in table:
            needle = ov.address_match.strip().lower()
            if needle and needle in haystack:
                return ov
    return None
