"""Revenue Health import + Enrollware site coverage / cannibalization logic."""
from __future__ import annotations

from app.ops.coverage import existing_site_coverage
from app.ops.revenue_health import (
    BREAK_EVEN_MONTHLY,
    enrich_health,
    load_revenue_health,
    nearest_site_health,
    revenue_health_summary,
)


# -- revenue health --------------------------------------------------------
def test_enrich_derives_break_even_gap_and_flags():
    row = enrich_health({"site_name": "X", "zip": "94538",
                         "health_status": "LEAKAGE",
                         "monthly_avg_revenue": "1558",
                         "period_net_profit": "-1200"})
    assert row["health_status"] == "LEAKAGE"
    assert row["break_even_gap"] == round(1558 - BREAK_EVEN_MONTHLY, 2)
    assert row["is_weak"] is True
    flags = " ".join(row["flags"]).lower()
    assert "leak" in flags
    assert "period" in flags          # monthly OK but period loss surfaced


def test_below_break_even_flag():
    row = enrich_health({"site_name": "Y", "zip": "94541",
                         "health_status": "CRITICAL",
                         "monthly_avg_revenue": "567",
                         "period_net_profit": "-2400"})
    assert any("break-even" in f.lower() for f in row["flags"])
    assert row["is_weak"] is True


def test_load_and_summary(tmp_path):
    p = tmp_path / "rh.csv"
    p.write_text("site_name,zip,health_status,monthly_avg_revenue,"
                 "period_net_profit\nA,94560,STRONG,4800,5000\n"
                 "B,94541,CRITICAL,567,-2400\n", encoding="utf-8")
    rows = load_revenue_health(p)
    assert len(rows) == 2
    summ = revenue_health_summary(rows)
    assert summ["by_status"]["STRONG"] == 1
    assert summ["by_status"]["CRITICAL"] == 1
    assert "B" in summ["at_risk_sites"]


def test_nearest_site_health_picks_closest():
    rows = [enrich_health({"site_name": "Near", "zip": "94560",
                           "health_status": "STRONG"}),
            enrich_health({"site_name": "Far", "zip": "99999",
                           "health_status": "WATCH"})]
    got = nearest_site_health("94541", rows=rows,
                              distance_fn=lambda a, b: 3.0 if a == "94560" else 50.0)
    assert got["site_name"] == "Near" and got["distance_miles"] == 3.0


# -- coverage / cannibalization -------------------------------------------
def _loc(name, zip_code, status="active", courses=None):
    return {"location_name": name, "zip": zip_code, "active_status": status,
            "courses_offered": courses or ["ARC_BLS"],
            "average_monthly_enrollment": 48}


def _health(name, zip_code, status, monthly=4800, period=5000):
    return enrich_health({"site_name": name, "zip": zip_code,
                          "health_status": status,
                          "monthly_avg_revenue": monthly,
                          "period_net_profit": period})


def test_strong_covering_site_use_existing_and_cannibalization():
    cov = existing_site_coverage(
        "94541", locations=[_loc("Newark", "94560")],
        health_rows=[_health("Newark", "94560", "STRONG")],
        distance_fn=lambda a, b: 3.0)
    assert cov["coverage_decision"] == "USE_EXISTING_SITE"
    assert cov["covered_by_existing"] is True
    assert cov["cannibalization_risk"] is True


def test_weak_covering_site_fix_first():
    cov = existing_site_coverage(
        "94541", locations=[_loc("Hayward", "94541")],
        health_rows=[_health("Hayward", "94541", "CRITICAL", monthly=567,
                             period=-2400)],
        distance_fn=lambda a, b: 2.0)
    assert cov["coverage_decision"] == "FIX_CURRENT_FIRST"
    assert cov["warnings"]


def test_strong_site_nearby_not_covering_is_cannibalization():
    cov = existing_site_coverage(
        "94541", locations=[_loc("Newark", "94560")],
        health_rows=[_health("Newark", "94560", "STRONG")],
        distance_fn=lambda a, b: 9.0)   # >6 (not covered), <12 (nearby)
    assert cov["coverage_decision"] == "CANNIBALIZATION_RISK"
    assert cov["covered_by_existing"] is False
    assert cov["cannibalization_risk"] is True


def test_no_nearby_site_open_test_ok():
    cov = existing_site_coverage(
        "94541", locations=[_loc("Faraway", "99999")],
        health_rows=[], distance_fn=lambda a, b: 40.0)
    assert cov["coverage_decision"] == "OPEN_TEST_OK"
    assert cov["covered_by_existing"] is False
