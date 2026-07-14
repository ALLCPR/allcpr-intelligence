"""
Unit economics & break-even for an ALLCPR / AUTOCPR training site.

Turns the abstract operating-feasibility score into dollars: at a given price
per student, how many students per month does a site need to break even, and
how does that compare to the real local demand and competitor pricing already
loaded by the ops layer?

The cost model is the real ALLCPR per-student / per-site cost structure (from
the Newark AUTOCPR cooperation accounting agreement):

    per student:  ARC cert/management fee   $18
                  SaaS / CRM / AI / SEO      $25
                  Smart Manikin consumables  $2
                  payment processing         3% of the student's price
    per site/mo:  fixed operating (rent, utilities, property)  $650
                  construction amortization ($10k over 24 mo)  $420
    per class:    instructor pay (roster salary)   ~$45 (course-overridable)

Every number is overridable: per-course price/card/instructor via the existing
``data/manual/course_economics.csv`` import; the shared site costs via an
optional ``data/manual/site_economics.csv`` (or ``.json``) single row. Missing
files → these documented defaults, never a crash.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import MANUAL_DIR
from app.ops.imports import load_course_economics, parse_float
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED, COURSE_LABELS
from app.ops.recruiting_policy import ENROLLMENT_TARGETS
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

SITE_ECONOMICS_FILE = MANUAL_DIR / "site_economics.csv"

# Real ALLCPR cost structure (documented above). Shared across courses.
DEFAULT_SITE_ECONOMICS: Dict[str, Any] = {
    "arc_cert_fee_per_student": 18.0,
    "saas_cost_per_student": 25.0,
    "consumables_per_student": 2.0,
    "payment_fee_pct": 0.03,
    "fixed_monthly_cost": 650.0,
    "construction_amortization_monthly": 420.0,
    "default_instructor_cost_per_class": 45.0,
    "avg_students_per_class": 7.0,
    "source": "allcpr_accounting_agreement_defaults",
}

# Fallback per-course price when no course_economics.csv override exists.
# (Conservative, aligned with the tracked example + competitor medians.)
_DEFAULT_COURSE_PRICE = {AHA_BLS: 95.0, ARC_BLS: 85.0, ARC_CPR_FA_AED: 75.0}
_DEFAULT_CARD_COST = {AHA_BLS: 25.0, ARC_BLS: 20.0, ARC_CPR_FA_AED: 18.0}


def load_site_economics(path: Path = SITE_ECONOMICS_FILE) -> Dict[str, Any]:
    """Shared per-site cost constants (first row wins); missing → defaults."""
    econ = dict(DEFAULT_SITE_ECONOMICS)
    p = Path(path)
    json_path = p.with_suffix(".json")
    rows: list = []
    try:
        if not p.exists() and json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            rows = data if isinstance(data, list) else [data]
        elif p.exists():
            with p.open("r", newline="", encoding="utf-8-sig") as fh:
                rows = [dict(r) for r in csv.DictReader(fh)]
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"site_economics: could not read {p}: {exc}")
        return econ
    if not rows:
        return econ
    raw = rows[0]
    for key in ("arc_cert_fee_per_student", "saas_cost_per_student",
                "consumables_per_student", "payment_fee_pct",
                "fixed_monthly_cost", "construction_amortization_monthly",
                "default_instructor_cost_per_class", "avg_students_per_class"):
        val = parse_float(raw.get(key))
        if val is not None:
            econ[key] = val
    econ["source"] = "manual_import"
    return econ


def _course_price(course: str, overrides: Dict[str, Dict[str, Any]]) -> float:
    row = overrides.get(course) or {}
    return (row.get("student_price")
            or _DEFAULT_COURSE_PRICE.get(course) or 75.0)


def _card_cost(course: str, overrides: Dict[str, Dict[str, Any]]) -> float:
    row = overrides.get(course) or {}
    val = row.get("card_cost")
    return val if val is not None else _DEFAULT_CARD_COST.get(course, 18.0)


def _instructor_per_class(course: str, overrides: Dict[str, Dict[str, Any]],
                          econ: Dict[str, Any]) -> float:
    row = overrides.get(course) or {}
    val = row.get("instructor_cost")
    return val if val is not None else econ["default_instructor_cost_per_class"]


def variable_cost_per_student(price: float, card_cost: float,
                              econ: Dict[str, Any]) -> float:
    """Per-student variable cost: cert/card + SaaS + consumables + payment %."""
    return round(
        card_cost
        + econ["saas_cost_per_student"]
        + econ["consumables_per_student"]
        + econ["payment_fee_pct"] * price,
        2,
    )


def course_unit_economics(course: str, price: float, card_cost: float,
                          instructor_per_class: float,
                          econ: Dict[str, Any]) -> Dict[str, Any]:
    """Per-student contribution + monthly break-even for one course."""
    per_class_students = max(1.0, econ["avg_students_per_class"])
    instructor_per_student = instructor_per_class / per_class_students
    variable = variable_cost_per_student(price, card_cost, econ)
    contribution = round(price - variable - instructor_per_student, 2)
    fixed_monthly = (econ["fixed_monthly_cost"]
                     + econ["construction_amortization_monthly"])
    break_even = (round(fixed_monthly / contribution, 1)
                  if contribution > 0 else None)
    return {
        "course": course,
        "course_label": COURSE_LABELS.get(course, course),
        "price_per_student": round(price, 2),
        "variable_cost_per_student": variable,
        "instructor_cost_per_student": round(instructor_per_student, 2),
        "contribution_margin_per_student": contribution,
        "contribution_margin_pct": (round(100 * contribution / price, 1)
                                    if price > 0 else None),
        "fixed_monthly_cost": round(fixed_monthly, 2),
        "break_even_students_per_month": break_even,
        "break_even_classes_per_month": (
            round(break_even / per_class_students, 1)
            if break_even is not None else None),
    }


def monthly_pnl(price: float, card_cost: float, instructor_per_class: float,
                students_per_month: float, econ: Dict[str, Any]
                ) -> Dict[str, Any]:
    """Full monthly P&L at a given student volume (all real cost lines)."""
    per_class_students = max(1.0, econ["avg_students_per_class"])
    classes = students_per_month / per_class_students
    revenue = price * students_per_month
    cert = _as_card(card_cost) * students_per_month
    saas = econ["saas_cost_per_student"] * students_per_month
    consumables = econ["consumables_per_student"] * students_per_month
    payment = econ["payment_fee_pct"] * revenue
    instructor = instructor_per_class * classes
    fixed = econ["fixed_monthly_cost"]
    amort = econ["construction_amortization_monthly"]
    total_cost = cert + saas + consumables + payment + instructor + fixed + amort
    net = revenue - total_cost
    return {
        "students_per_month": round(students_per_month, 1),
        "revenue": round(revenue, 2),
        "costs": {
            "cert_card": round(cert, 2),
            "saas": round(saas, 2),
            "consumables": round(consumables, 2),
            "payment_fee": round(payment, 2),
            "instructor": round(instructor, 2),
            "fixed_operating": round(fixed, 2),
            "construction_amortization": round(amort, 2),
            "total": round(total_cost, 2),
        },
        "net_profit": round(net, 2),
        "net_margin_pct": round(100 * net / revenue, 1) if revenue > 0 else None,
    }


def _as_card(card_cost: float) -> float:
    return card_cost if card_cost is not None else 18.0


def site_economics(zip_code: str,
                   competitor_ctx: Optional[Dict[str, Any]] = None,
                   demand_ctx: Optional[Dict[str, Any]] = None,
                   course_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
                   econ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-ZIP dollar view: per-course break-even + a demand-vs-break-even read.

    Uses the competitor median price for a course when available (real local
    pricing) and the 6-month local student demand to judge whether a site can
    realistically clear break-even.
    """
    econ = econ or load_site_economics()
    course_overrides = (course_overrides if course_overrides is not None
                        else load_course_economics())
    competitor_ctx = competitor_ctx or {}
    demand_ctx = demand_ctx or {}

    comp_price_by_course = {}
    for row in competitor_ctx.get("courses", []) or []:
        ct = str(row.get("course_type") or "").upper()
        med = row.get("median_price")
        if med:
            # Competitor "BLS" maps to both AHA_BLS and ARC_BLS pricing bands.
            if ct == "BLS":
                comp_price_by_course.setdefault(AHA_BLS, med)
                comp_price_by_course.setdefault(ARC_BLS, med)
            elif ct == "CPR":
                comp_price_by_course.setdefault(ARC_CPR_FA_AED, med)

    courses = []
    for course in (AHA_BLS, ARC_BLS, ARC_CPR_FA_AED):
        override_price = (course_overrides.get(course) or {}).get("student_price")
        price = override_price or comp_price_by_course.get(course) \
            or _course_price(course, course_overrides)
        price_source = ("manual_override" if override_price
                        else "competitor_median" if course in comp_price_by_course
                        else "default")
        ce = course_unit_economics(
            course, price, _card_cost(course, course_overrides),
            _instructor_per_class(course, course_overrides, econ), econ)
        ce["price_source"] = price_source
        courses.append(ce)

    # Demand vs break-even, using the cheapest (easiest) course to clear.
    students_6mo = demand_ctx.get("student_count") or 0
    students_per_month = round(students_6mo / 6.0, 1) if students_6mo else 0.0
    best = min((c for c in courses
                if c["break_even_students_per_month"] is not None),
               key=lambda c: c["break_even_students_per_month"], default=None)
    demand_read = None
    if best and students_6mo:
        be = best["break_even_students_per_month"]
        pct = round(100 * students_per_month / be, 0) if be else None
        target = ENROLLMENT_TARGETS["site_students_per_month"]
        demand_read = {
            "local_students_per_month": students_per_month,
            "easiest_break_even_course": best["course_label"],
            "easiest_break_even_students_per_month": be,
            "demand_vs_break_even_pct": pct,
            "clears_break_even": (students_per_month >= be) if be else None,
            # ALLCPR's own site enrollment target (25/wk ≈ 108/mo) as the bar
            # a healthy site should reach, well above bare break-even.
            "company_site_target_per_month": target,
            "meets_company_target": students_per_month >= target,
        }

    return {
        "zip": str(zip_code).zfill(5),
        "cost_model_source": econ.get("source"),
        "fixed_monthly_cost": round(
            econ["fixed_monthly_cost"]
            + econ["construction_amortization_monthly"], 2),
        "courses": courses,
        "demand_read": demand_read,
    }
