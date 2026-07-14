"""
Tests for the dollar-grounded unit-economics / break-even model.

Pins the real ALLCPR cost structure (per-student cert/SaaS/consumables/payment,
per-site fixed + amortization), the break-even math, and the demand-vs-break-
even read that uses competitor pricing + real local demand.
"""
from __future__ import annotations

from app.ops import unit_economics as ue
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED, scrub_sensitive


def _econ():
    return dict(ue.DEFAULT_SITE_ECONOMICS)


def test_default_cost_structure_matches_agreement():
    e = ue.DEFAULT_SITE_ECONOMICS
    assert e["arc_cert_fee_per_student"] == 18.0
    assert e["saas_cost_per_student"] == 25.0
    assert e["consumables_per_student"] == 2.0
    assert e["payment_fee_pct"] == 0.03
    assert e["fixed_monthly_cost"] == 650.0
    assert e["construction_amortization_monthly"] == 420.0


def test_variable_cost_per_student_includes_payment_pct():
    e = _econ()
    # card 18 + saas 25 + consumables 2 + 3% of $100 = 48
    assert ue.variable_cost_per_student(100.0, 18.0, e) == 48.0


def test_course_unit_economics_break_even_math():
    e = _econ()
    ce = ue.course_unit_economics(ARC_BLS, price=85.0, card_cost=18.0,
                                  instructor_per_class=45.0, econ=e)
    # variable = 18 + 25 + 2 + 0.03*85 = 47.55
    assert ce["variable_cost_per_student"] == 47.55
    # instructor per student = 45 / 7 = 6.43
    assert ce["instructor_cost_per_student"] == 6.43
    # contribution = 85 - 47.55 - 6.43 = 31.02
    assert ce["contribution_margin_per_student"] == 31.02
    # fixed = 650 + 420 = 1070; break-even = 1070 / 31.02 ≈ 34.5
    assert ce["fixed_monthly_cost"] == 1070.0
    assert abs(ce["break_even_students_per_month"] - 34.5) < 0.2


def test_non_positive_contribution_has_no_break_even():
    e = _econ()
    # Price below variable+instructor cost → cannot break even.
    ce = ue.course_unit_economics(AHA_BLS, price=30.0, card_cost=25.0,
                                  instructor_per_class=45.0, econ=e)
    assert ce["contribution_margin_per_student"] <= 0
    assert ce["break_even_students_per_month"] is None
    assert ce["break_even_classes_per_month"] is None


def test_monthly_pnl_all_cost_lines_and_net():
    e = _econ()
    pnl = ue.monthly_pnl(price=85.0, card_cost=18.0, instructor_per_class=45.0,
                         students_per_month=70.0, econ=e)
    assert pnl["revenue"] == 85.0 * 70
    costs = pnl["costs"]
    assert costs["saas"] == 25.0 * 70
    assert costs["consumables"] == 2.0 * 70
    assert costs["payment_fee"] == round(0.03 * 85.0 * 70, 2)
    assert costs["fixed_operating"] == 650.0
    assert costs["construction_amortization"] == 420.0
    # net = revenue - total
    assert pnl["net_profit"] == round(pnl["revenue"] - costs["total"], 2)


def test_site_economics_uses_competitor_median_price():
    competitor = {"courses": [
        {"course_type": "BLS", "median_price": 90},
        {"course_type": "CPR", "median_price": 70},
    ]}
    demand = {"student_count": 60}   # 60 over 6 months = 10/month
    econ = ue.site_economics("95112", competitor_ctx=competitor,
                             demand_ctx=demand, course_overrides={})
    by_course = {c["course"]: c for c in econ["courses"]}
    assert by_course[AHA_BLS]["price_per_student"] == 90.0
    assert by_course[AHA_BLS]["price_source"] == "competitor_median"
    assert by_course[ARC_CPR_FA_AED]["price_per_student"] == 70.0
    dr = econ["demand_read"]
    assert dr["local_students_per_month"] == 10.0
    assert dr["clears_break_even"] is False   # 10/mo well below break-even
    assert dr["demand_vs_break_even_pct"] is not None


def test_course_override_price_beats_competitor():
    competitor = {"courses": [{"course_type": "BLS", "median_price": 90}]}
    overrides = {AHA_BLS: {"student_price": 120.0, "card_cost": 25.0,
                           "instructor_cost": 180.0}}
    econ = ue.site_economics("95112", competitor_ctx=competitor,
                             demand_ctx={}, course_overrides=overrides)
    aha = next(c for c in econ["courses"] if c["course"] == AHA_BLS)
    assert aha["price_per_student"] == 120.0
    assert aha["price_source"] == "manual_override"


def test_site_economics_no_data_still_produces_defaults():
    econ = ue.site_economics("99999", competitor_ctx={}, demand_ctx={},
                             course_overrides={})
    assert len(econ["courses"]) == 3
    assert all(c["price_source"] == "default" for c in econ["courses"])
    assert econ["demand_read"] is None   # no demand → no read


def test_economics_payload_scrubs_clean():
    econ = ue.site_economics("95112", competitor_ctx={}, demand_ctx={},
                             course_overrides={})
    scrubbed = scrub_sensitive({"unit_economics": econ})
    assert scrubbed["unit_economics"]["courses"]
    assert "door_code" not in str(scrubbed)


def test_load_site_economics_defaults_when_missing(tmp_path):
    econ = ue.load_site_economics(tmp_path / "nope.csv")
    assert econ["arc_cert_fee_per_student"] == 18.0
    assert econ["source"] == "allcpr_accounting_agreement_defaults"


def test_load_site_economics_override(tmp_path):
    import csv
    p = tmp_path / "site_economics.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["saas_cost_per_student", "fixed_monthly_cost"])
        w.writerow(["30", "800"])
    econ = ue.load_site_economics(p)
    assert econ["saas_cost_per_student"] == 30.0
    assert econ["fixed_monthly_cost"] == 800.0
    assert econ["source"] == "manual_import"
