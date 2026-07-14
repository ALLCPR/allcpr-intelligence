"""
Revenue Health layer — site profitability / break-even as an expansion input.

A lightweight CSV import of the Revenue Health workbook (NOT Excel rendering):
per-site health status (CRITICAL / WATCH / STABLE / STRONG / LEAKAGE), monthly
average revenue vs the ~$1,067/month break-even line, trend, responsible team,
and the AutoCPR "monthly healthy but period loss" nuance.

Used by the coverage/expansion logic to answer: is the nearest existing site
strong (→ cannibalization risk if we open next door), weak/critical (→ fix the
current site before expanding), or leaking (→ expansion risk)?

Real data lives in the gitignored ``data/manual/site_revenue_health.csv``; only
``site_revenue_health.csv.example`` is tracked.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import MANUAL_DIR
from app.ops.instructor_supply import distance_between_zips
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

REVENUE_HEALTH_FILE = MANUAL_DIR / "site_revenue_health.csv"

# The break-even line from the AutoCPR/AUTOCPR accounting agreement (~$1,067/mo:
# $650 rent + $420 construction-amort + consumables).
BREAK_EVEN_MONTHLY = 1067.0

CRITICAL, WATCH, STABLE, STRONG, LEAKAGE = (
    "CRITICAL", "WATCH", "STABLE", "STRONG", "LEAKAGE")
HEALTH_STATUSES = (CRITICAL, WATCH, STABLE, STRONG, LEAKAGE)

# Plain-English wording for the dashboard.
_HEALTH_LABEL = {
    CRITICAL: "Critical", WATCH: "Watch", STABLE: "Stable",
    STRONG: "Strong", LEAKAGE: "Leakage",
}
# Statuses that make expanding *near* this site risky in different ways.
_WEAK = frozenset({CRITICAL, LEAKAGE})
_STRONG = frozenset({STRONG})


def _f(value: Any) -> Optional[float]:
    text = str(value or "").replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _s(value: Any) -> str:
    return str(value or "").strip()


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except OSError as exc:
        logger.warning(f"revenue_health: could not read {p}: {exc}")
        return []


def health_flags(row: Dict[str, Any]) -> List[str]:
    """Plain warnings from a normalized revenue-health row."""
    flags: List[str] = []
    status = row.get("health_status")
    if status == LEAKAGE:
        flags.append("Revenue leakage at this site")
    elif status == CRITICAL:
        flags.append("Site revenue is critical")
    gap = row.get("break_even_gap")
    if gap is not None and gap < 0:
        flags.append(f"Below break-even by ${abs(round(gap))}/mo")
    monthly = row.get("monthly_avg_revenue")
    period = row.get("period_net_profit")
    if (monthly is not None and monthly >= BREAK_EVEN_MONTHLY
            and period is not None and period < 0):
        flags.append("Monthly health OK but period shows a net loss")
    return flags


def enrich_health(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a raw CSV row: parse numbers, derive break-even gap + flags."""
    status = _s(row.get("health_status")).upper()
    if status not in HEALTH_STATUSES:
        status = WATCH
    monthly = _f(row.get("monthly_avg_revenue"))
    gap = _f(row.get("break_even_gap"))
    if gap is None and monthly is not None:
        gap = round(monthly - BREAK_EVEN_MONTHLY, 2)
    out = {
        "site_name": _s(row.get("site_name")),
        "location_id": _s(row.get("location_id")),
        "address": _s(row.get("address")),
        "city": _s(row.get("city")),
        "state": _s(row.get("state")),
        "zip": _s(row.get("zip")).zfill(5) if _s(row.get("zip")) else "",
        "health_status": status,
        "health_label": _HEALTH_LABEL.get(status, status.title()),
        "priority": _s(row.get("priority")),
        "trend": _s(row.get("trend")),
        "june_students": _f(row.get("june_students")),
        "june_revenue": _f(row.get("june_revenue")),
        "six_month_students": _f(row.get("six_month_students")),
        "six_month_revenue": _f(row.get("six_month_revenue")),
        "active_months": _f(row.get("active_months")),
        "monthly_avg_revenue": monthly,
        "break_even_gap": gap,
        "free_gifted_students": _f(row.get("free_gifted_students")),
        "unit_contribution": _f(row.get("unit_contribution")),
        "why": _s(row.get("why")),
        "action": _s(row.get("action")),
        "responsible_team": _s(row.get("responsible_team")),
        "action_due_date": _s(row.get("action_due_date")),
        "autocpr_flag": _s(row.get("autocpr_flag")).lower() in (
            "1", "true", "yes", "y"),
        "period_net_profit": _f(row.get("period_net_profit")),
        "investor_share": _f(row.get("investor_share")),
        "company_share": _f(row.get("company_share")),
        "is_weak": status in _WEAK,
        "is_strong": status in _STRONG,
    }
    out["flags"] = health_flags(out)
    return out


def load_revenue_health(path: Path = REVENUE_HEALTH_FILE
                        ) -> List[Dict[str, Any]]:
    """All revenue-health sites (normalized). Empty when the file is absent."""
    return [enrich_health(r) for r in _load_rows(path)
            if _s(r.get("site_name"))]


def revenue_health_summary(rows: Optional[List[Dict[str, Any]]] = None
                           ) -> Dict[str, Any]:
    """Portfolio rollup: counts by status + leakage/critical call-outs."""
    rows = load_revenue_health() if rows is None else rows
    by_status = {s: 0 for s in HEALTH_STATUSES}
    for r in rows:
        by_status[r["health_status"]] = by_status.get(r["health_status"], 0) + 1
    at_risk = [r["site_name"] for r in rows if r["is_weak"]]
    return {
        "total_sites": len(rows),
        "by_status": by_status,
        "at_risk_sites": at_risk,
        "break_even_monthly": BREAK_EVEN_MONTHLY,
    }


def nearest_site_health(
    zip_code: str,
    *,
    rows: Optional[List[Dict[str, Any]]] = None,
    distance_fn: Optional[Callable[[str, str], Optional[float]]] = None,
) -> Optional[Dict[str, Any]]:
    """The revenue-health site nearest a ZIP (with distance), or None."""
    rows = load_revenue_health() if rows is None else rows
    distance_fn = distance_fn or distance_between_zips
    zip_code = str(zip_code).zfill(5)
    best: Optional[Dict[str, Any]] = None
    best_dist = float("inf")
    for r in rows:
        if not r.get("zip"):
            continue
        dist = distance_fn(r["zip"], zip_code)
        d = dist if dist is not None else 9999.0
        if d < best_dist:
            best, best_dist = r, d
    if best is None:
        return None
    out = dict(best)
    out["distance_miles"] = None if best_dist >= 9999.0 else round(best_dist, 1)
    return out
