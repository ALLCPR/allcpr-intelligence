"""Tests for translating a score into a course recommendation (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.course_recommendation import (  # noqa: E402
    AVOID,
    EXPAND,
    MAINTAIN,
    TEST_ONLY,
    CourseRecommendation,
    recommend_course,
)


def test_high_score_is_expand_primary():
    r = recommend_course(75.0)
    assert r.action == EXPAND
    assert r.display_group == "Primary"


def test_mid_score_is_maintain_secondary():
    r = recommend_course(60.0)
    assert r.action == MAINTAIN
    assert r.display_group == "Secondary"


def test_low_score_is_test_only():
    r = recommend_course(40.0)
    assert r.action == TEST_ONLY
    assert r.display_group == "Avoid / test only"


def test_very_low_score_is_avoid():
    r = recommend_course(15.0)
    assert r.action == AVOID
    assert r.display_group == "Avoid / test only"


def test_thresholds_are_inclusive_lower_bounds():
    assert recommend_course(70.0).action == EXPAND
    assert recommend_course(69.99).action == MAINTAIN
    assert recommend_course(50.0).action == MAINTAIN
    assert recommend_course(49.99).action == TEST_ONLY
    assert recommend_course(30.0).action == TEST_ONLY
    assert recommend_course(29.99).action == AVOID


def test_reasons_are_passed_through():
    r = recommend_course(75.0, reasons=["Above local average."])
    assert "Above local average." in r.reasons


def test_to_dict_is_json_ready():
    r = recommend_course(60.0, course_label="ARC CPR")
    d = r.to_dict()
    assert d["action"] == MAINTAIN
    assert d["display_group"] == "Secondary"
    assert "label" in d and "reasons" in d
