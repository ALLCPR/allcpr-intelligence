"""Tests for the executive report UX / interpretation upgrade."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

from app.reports.html_report import render_html_report
from app.reports.interpretation import (
    STRATEGY_KEYS,
    build_candidate_interpretation,
    build_report_interpretation,
    candidate_matches_strategies,
    candidate_strategy_keys,
    demand_signals_ranked,
    expansion_readiness,
    score_bar,
    strategy_recommendations,
)
from app.reports.json_report import render_json
from app.reports.markdown_report import render_markdown_report
from app.scoring.site_score import score_profile
from app.utils.source_audit import build_compact_source_audit
from scripts import full_pipeline
from tests.test_phase2 import _profile


# ----- expansion readiness rules --------------------------------------------

def _scored(site=85.0, conf=70.0, gap=70.0, sat=0.2,
            rent="manual_override"):
    return {
        "site_score": site,
        "sub_scores": {"confidence_score": conf, "competition_gap_score": gap},
        "rent": {"rent_data_confidence": rent},
        "competition_breakdown": {"effective_saturation": sat},
    }


def test_readiness_strong_when_all_high_and_rent_known():
    assert expansion_readiness(_scored())["readiness"] == "Strong"


def test_readiness_moderate_band():
    assert expansion_readiness(_scored(site=70, conf=50))["readiness"] == "Moderate"


def test_readiness_weak_when_below_bar():
    assert expansion_readiness(_scored(site=40, conf=40))["readiness"] == "Weak"


def test_readiness_unknown_rent_caps_at_moderate():
    # Strong inputs except rent is unknown -> cannot exceed Moderate.
    assert expansion_readiness(_scored(rent="unknown"))["readiness"] == "Moderate"
    # When the cap actually lowers readiness, the reason explains rent.
    out = expansion_readiness(_scored(sat=0.95, gap=20, rent="unknown"))
    assert out["readiness"] == "Moderate"


def test_readiness_low_confidence_caps_at_weak():
    assert expansion_readiness(_scored(conf=20))["readiness"] == "Weak"


def test_readiness_saturated_low_gap_caps_at_moderate():
    out = expansion_readiness(_scored(sat=0.9, gap=20))
    assert out["readiness"] == "Moderate"


# ----- demand importance ordering -------------------------------------------

def test_demand_signals_rank_by_business_importance_not_count():
    profile = {"counts_5mi": {"gym": 40, "hospital": 1, "childcare_center": 6}}
    ds = demand_signals_ranked(profile)
    # Hospital (Very high) must outrank a gym with far more results.
    assert ds["high_value"][0]["key"] == "hospital"
    assert all(r["key"] != "gym" for r in ds["high_value"])
    assert any(r["key"] == "gym" for r in ds["secondary"])
    assert any(r["key"] == "childcare_center" for r in ds["secondary"])


# ----- strategy label selection ---------------------------------------------

def test_strategy_picks_nursing_hub_and_caps_at_three():
    profile = {
        "counts_5mi": {"nursing_school": 9, "hospital": 3, "fire_station": 2},
        "competition_summary": {"competitor_count_by_bucket_mi": {5: 30}},
    }
    scored = {"competition_breakdown": {"effective_saturation": 0.95}}
    strategies = strategy_recommendations(profile, scored)
    labels = [s["label"] for s in strategies]
    assert "Nursing Student Certification Hub" in labels
    assert "Partnership-First Market Entry" in labels
    assert len(strategies) <= 3


def test_strategy_airport_corridor_detected_within_one_mile():
    profile = {
        "counts_5mi": {"hospital": 1},
        "accessibility": {"signals": {
            "airport_business_corridor_proximity": {
                "status": "detected", "distance_miles": 0.6,
            }
        }},
        "competition_summary": {"competitor_count_by_bucket_mi": {5: 1}},
    }
    scored = {"competition_breakdown": {"effective_saturation": 0.1}}
    labels = [s["label"] for s in strategy_recommendations(profile, scored)]
    assert "Airport / Corporate Workforce CPR Center" in labels


def test_strategy_always_returns_at_least_one():
    profile = {"counts_5mi": {}, "competition_summary": {}}
    scored = {"competition_breakdown": {"effective_saturation": 0.0}}
    assert strategy_recommendations(profile, scored)


# ----- compact source audit aggregation -------------------------------------

def test_compact_source_audit_aggregates_website_fetches():
    sources = [
        {"name": "unknown", "url": "https://a.example", "fields": []},
        {"name": "unknown", "url": "https://b.example", "fields": []},
        {"name": "unknown", "url": "https://c.example", "fields": []},
        {"name": "Google Places API (Nearby Search)",
         "url": "https://maps.googleapis.com/x", "fields": ["nearby_hospital"]},
        {"name": "US Census Bureau ACS 5-year",
         "url": "https://api.census.gov/x", "fields": ["population"]},
    ]
    rows = build_compact_source_audit(sources)
    families = {r["source"]: r for r in rows}
    assert "Competitor Website Fetch" in families
    # Three separate website fetches collapse into one row.
    assert families["Competitor Website Fetch"]["records"] == 3
    assert "Google Places Nearby Search" in families
    assert "Census ACS" in families
    # One compact row per family — not one row per raw record.
    assert len(rows) == 3


# ----- score bar ------------------------------------------------------------

def test_score_bar_fills_proportionally():
    assert score_bar(100, 10) == "█" * 10
    assert score_bar(0, 10) == "░" * 10
    assert "█" in score_bar(50, 10) and "░" in score_bar(50, 10)
    assert "unknown" in score_bar(None, 10)


# ----- executive verdict generation -----------------------------------------

def test_executive_verdict_generated_from_real_inputs():
    profile = _profile()
    scored = score_profile(profile)
    report_interp = build_report_interpretation([(profile, scored)])
    ev = report_interp["executive_verdict"]
    for key in ("best_candidate", "verdict", "expansion_readiness",
                "why_it_matters", "biggest_risk", "best_strategy",
                "confidence", "before_leasing"):
        assert key in ev and ev[key]
    assert ev["best_candidate"] == profile["anchor"]["name"]
    assert ev["expansion_readiness"] in ("Strong", "Moderate", "Weak")
    assert len(report_interp["next_actions"]) == 3


def test_candidate_interpretation_bundle_is_complete():
    profile = _profile()
    scored = score_profile(profile)
    interp = build_candidate_interpretation(profile, scored)
    for key in ("expansion_readiness", "demand_signals", "strategies",
                "competitor_interpretation", "warnings", "score_meters",
                "decision_checklist", "quick_read"):
        assert key in interp


# ----- markdown executive style ---------------------------------------------

def test_markdown_executive_has_verdict_and_no_giant_source_rows():
    profile = _profile()
    scored = score_profile(profile)
    md = render_markdown_report("Testville", "CA", 2.0, [(profile, scored)],
                                report_style="executive")
    assert "## Executive verdict" in md
    assert "Recommended next 3 actions" in md
    assert "Quick read" in md
    assert "Expansion readiness" in md
    assert "Decision checklist before leasing" in md
    assert "Source audit (compact)" in md
    # Executive style must NOT dump the giant per-field source audit appendix.
    assert "Source audit appendix" not in md
    assert "Source API / URL" not in md
    assert "SECRET" not in md


def test_markdown_debug_style_includes_full_appendix():
    profile = _profile()
    scored = score_profile(profile)
    md = render_markdown_report("Testville", "CA", 2.0, [(profile, scored)],
                                report_style="debug")
    assert "Source audit appendix (debug)" in md
    assert "Raw diagnostics (debug)" in md
    assert "SECRET" not in md


# ----- html dashboard -------------------------------------------------------

def test_html_renders_score_bars_and_collapsible_audit():
    profile = _profile()
    scored = score_profile(profile)
    payload = render_json([(profile, scored)],
                          context={"mode": "metro_comparison"})
    html = render_html_report(payload)
    assert "meter-fill" in html          # styled score bars
    assert "meter-track" in html
    assert "<details" in html            # collapsible raw detail
    assert "exec-panel" in html          # sticky executive summary
    assert "badge tier-" in html         # colored tier badges
    assert "Executive verdict" in html
    assert "SECRET" not in html


def test_html_no_api_key_leakage_executive_and_debug():
    profile = _profile()
    scored = score_profile(profile)
    payload = render_json([(profile, scored)], context={})
    for style in ("executive", "detailed", "debug"):
        html = render_html_report(payload, report_style=style)
        assert "SECRET" not in html
        assert "fake-key-for-tests" not in html


# ----- json keeps full detail + interpretation ------------------------------

def test_json_keeps_full_detail_and_adds_interpretation():
    profile = _profile()
    scored = score_profile(profile)
    payload = render_json([(profile, scored)], context={"mode": "city"})
    candidate = payload["candidates"][0]
    # Full detailed data is still present.
    assert candidate["profile"]["competitors"]
    assert candidate["scored"]["sub_scores"]
    # Interpretation is added alongside, not instead of, the raw data.
    assert candidate["interpretation"]["expansion_readiness"]["readiness"]
    assert payload["report_interpretation"]["executive_verdict"]


# ----- CLI report-style flag ------------------------------------------------

def test_cli_report_style_defaults_to_executive():
    args = full_pipeline.parse_args(["--cities", "targets.txt"])
    assert args.report_style == "executive"


def test_cli_report_style_accepts_detailed_and_debug():
    for style in ("detailed", "debug", "executive"):
        args = full_pipeline.parse_args(
            ["--cities", "targets.txt", "--report-style", style]
        )
        assert args.report_style == style


# ----- --fit-strategy report filter -----------------------------------------

def _nursing_profile():
    return {"counts_5mi": {"nursing_school": 5}, "competition_summary": {}}


def _childcare_profile():
    return {"counts_5mi": {"childcare_center": 8}, "competition_summary": {}}


_LOW_SAT = {"competition_breakdown": {"effective_saturation": 0.1}}


def test_strategy_keys_are_known_and_stable():
    keys = candidate_strategy_keys(_nursing_profile(), _LOW_SAT)
    assert "nursing" in keys
    assert keys <= set(STRATEGY_KEYS.values())


def test_candidate_matches_strategies():
    p, s = _nursing_profile(), _LOW_SAT
    assert candidate_matches_strategies(p, s, {"nursing"}) is True
    assert candidate_matches_strategies(p, s, {"childcare"}) is False
    # An empty filter means "no filter" — everything matches.
    assert candidate_matches_strategies(p, s, set()) is True


def test_parse_fit_keys_validates_and_drops_unknown():
    assert full_pipeline._parse_fit_keys("nursing, hospital") == {"nursing", "hospital"}
    assert full_pipeline._parse_fit_keys("nursing,bogus") == {"nursing"}
    assert full_pipeline._parse_fit_keys("") == set()


def test_filter_fit_keeps_only_matching_areas():
    ranked = [(_nursing_profile(), _LOW_SAT), (_childcare_profile(), _LOW_SAT)]
    filtered = full_pipeline._filter_fit(ranked, {"nursing"})
    assert len(filtered) == 1
    assert filtered[0][0]["counts_5mi"].get("nursing_school") == 5
    # No filter -> list returned unchanged.
    assert full_pipeline._filter_fit(ranked, set()) == ranked


def test_cli_fit_strategy_flag_parsed():
    args = full_pipeline.parse_args(
        ["--cities", "targets.txt", "--fit-strategy", "nursing,hospital"]
    )
    assert args.fit_strategy == "nursing,hospital"
    assert full_pipeline.parse_args(["--cities", "t.txt"]).fit_strategy == ""
