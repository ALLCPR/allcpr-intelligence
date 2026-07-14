"""
ALLCPR per-state actual course prices.

Replaces the global $85/$95 defaults in the profitability model with
**real prices observed in ALLCPR's own class records** (extracted from the
QA report). Two-tier lookup:

  1. Per-state median, when at least 2 reliable samples exist for that state
     (drops $0 anomalies + n=1 single-sample states for stability).
  2. Overall median across all ALLCPR class records (currently $79).

This is a configuration override, not a fetched API — the data lives in
``data/raw/allcpr_actual_prices.csv`` (gitignored, proprietary). When the
file is absent, the lookup gracefully falls back to the legacy config
defaults so the pipeline still runs.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from app.config import AVG_BLS_COURSE_PRICE, AVG_CPR_COURSE_PRICE, RAW_DIR
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

PRICES_FILE = RAW_DIR / "allcpr_actual_prices.csv"


@dataclass
class PriceLookup:
    avg_price: float
    source: str          # "state:<XX>" | "overall_median" | "config_default"
    sample_size: int     # 0 when falling back to config_default


def _load() -> Dict[str, dict]:
    """Return {state: {median_price, sample_size, overall_median}} or {}."""
    if not Path(PRICES_FILE).exists():
        return {}
    try:
        with open(PRICES_FILE, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        logger.warning(f"allcpr_prices: failed to read {PRICES_FILE}: {exc}")
        return {}
    out: Dict[str, dict] = {}
    for r in rows:
        st = (r.get("state") or "").strip().upper()
        if not st:
            continue
        try:
            out[st] = {
                "median_price": float(r.get("median_price") or 0.0),
                "sample_size": int(r.get("sample_size") or 0),
                "overall_median": float(r.get("overall_median") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return out


_CACHE: Optional[Dict[str, dict]] = None


def _table() -> Dict[str, dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def reload() -> None:
    """Force re-read on next lookup (for tests)."""
    global _CACHE
    _CACHE = None


def lookup_price(state: Optional[str]) -> PriceLookup:
    """Return the best available avg course price for a candidate's state.

    Order of preference:
      1. State median (when n>=2 in the loaded table).
      2. Overall ALLCPR median (when any rows exist).
      3. Legacy config default ($85 + $95)/2 = $90 — only when the file is
         missing entirely.
    """
    table = _table()
    if table:
        overall = next(iter(table.values()))["overall_median"]
        if state:
            st = state.strip().upper()
            row = table.get(st)
            if row and row["median_price"] > 0 and row["sample_size"] >= 2:
                return PriceLookup(
                    avg_price=row["median_price"],
                    source=f"state:{st}",
                    sample_size=int(row["sample_size"]),
                )
        if overall > 0:
            return PriceLookup(
                avg_price=overall,
                source="overall_median",
                sample_size=sum(int(r["sample_size"]) for r in table.values()),
            )
    fallback = (AVG_CPR_COURSE_PRICE + AVG_BLS_COURSE_PRICE) / 2.0
    return PriceLookup(
        avg_price=fallback,
        source="config_default",
        sample_size=0,
    )
