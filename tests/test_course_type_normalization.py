"""Bucket mapper tests: ARC_CPR / ARC_BLS / AHA_BLS / OTHER.

The mapper is a thin layer over app.enrichers.course_classifier — these tests
pin the fold decisions (Heartsaver/blended -> ARC_CPR) and the guard rules
(ARC BLS never AHA BLS; CPR never BLS; BLS never CPR).
"""
import pytest

from app.enrichers.course_classifier import classify_course_type
from app.scoring.course_types import (
    AHA_BLS,
    ARC_BLS,
    ARC_CPR,
    BUCKET_LABELS,
    OTHER,
    REPORT_BUCKETS,
    demand_strength_category,
    to_demand_bucket,
)


def bucket_for(class_name: str) -> str:
    """Classify with the real classifier, then map — the production path."""
    return to_demand_bucket(classify_course_type(class_name), class_name)


# ---- ARC_CPR bucket (incl. documented folds) ------------------------------ #

@pytest.mark.parametrize("name", [
    "ARC Adult CPR/AED",
    "ARC Adult and Pediatric CPR/AED",
    "Red Cross Adult First Aid/CPR/AED",
    "Red Cross First Aid",
])
def test_arc_cpr_first_aid_maps_to_arc_cpr(name):
    assert bucket_for(name) == ARC_CPR


def test_blended_cpr_folds_into_arc_cpr():
    # provider-less blended CPR/First Aid -> cpr_first_aid_blended -> ARC_CPR
    assert classify_course_type(
        "Self Directed Adult First Aid/CPR/AED") == "cpr_first_aid_blended"
    assert bucket_for("Self Directed Adult First Aid/CPR/AED") == ARC_CPR


def test_aha_heartsaver_folds_into_arc_cpr():
    # documented simplification: bucket = general CPR/FA demand, not ARC-only
    assert classify_course_type("AHA Heartsaver CPR AED") == "aha_cpr"
    assert bucket_for("AHA Heartsaver CPR AED") == ARC_CPR


# ---- ARC_BLS / AHA_BLS — never mixed --------------------------------------- #

def test_arc_bls_maps_to_arc_bls():
    assert bucket_for("ARC BLS for Healthcare Providers") == ARC_BLS
    assert bucket_for("Red Cross Basic Life Support") == ARC_BLS


def test_aha_bls_maps_to_aha_bls():
    assert bucket_for("AHA BLS Provider") == AHA_BLS
    assert bucket_for("American Heart Association BLS") == AHA_BLS


def test_arc_and_aha_bls_never_swap():
    assert bucket_for("ARC BLS for Healthcare Providers") != AHA_BLS
    assert bucket_for("AHA BLS Provider") != ARC_BLS


# ---- skills-session routing ------------------------------------------------ #

def test_arc_bls_skills_session_routes_to_arc_bls():
    assert classify_course_type("Red Cross BLS Skills Session") == "skills_session"
    assert bucket_for("Red Cross BLS Skills Session") == ARC_BLS


def test_aha_bls_skills_session_routes_to_aha_bls():
    assert bucket_for("AHA BLS Skills Session") == AHA_BLS


def test_providerless_bls_skills_session_is_other():
    assert bucket_for("BLS Skills Session") == OTHER


def test_non_bls_skills_session_is_other():
    assert bucket_for("ARC CPR Skills Session") == OTHER


# ---- CPR is never BLS; BLS is never CPR ------------------------------------ #

def test_generic_cpr_is_never_bls():
    assert bucket_for("AHA Heartsaver CPR AED") not in (ARC_BLS, AHA_BLS)
    assert bucket_for("ARC Adult CPR/AED") not in (ARC_BLS, AHA_BLS)


def test_bls_is_never_cpr():
    assert bucket_for("ARC BLS for Healthcare Providers") != ARC_CPR
    assert bucket_for("AHA BLS Provider") != ARC_CPR


# ---- OTHER ------------------------------------------------------------------ #

@pytest.mark.parametrize("name", [
    "ALLCPR BLS Provider Course",
    "ALLCPR CPR Class",
    "AHA ACLS Provider",
    "PALS Renewal",
    "Babysitting Basics",
])
def test_house_brand_advanced_and_unknown_are_other(name):
    assert bucket_for(name) == OTHER


# ---- constants + strength bands --------------------------------------------- #

def test_report_buckets_are_exactly_three():
    assert REPORT_BUCKETS == (ARC_CPR, ARC_BLS, AHA_BLS)
    assert BUCKET_LABELS[ARC_CPR] == "ARC CPR"   # label intentionally unchanged
    assert BUCKET_LABELS[ARC_BLS] == "ARC BLS"
    assert BUCKET_LABELS[AHA_BLS] == "AHA BLS"


@pytest.mark.parametrize("score,expected", [
    (100, "Very Strong"), (90, "Very Strong"),
    (89, "Strong"), (75, "Strong"),
    (74, "Moderate"), (60, "Moderate"),
    (59, "Weak"), (40, "Weak"),
    (39, "Very Weak"), (0, "Very Weak"),
])
def test_demand_strength_bands(score, expected):
    assert demand_strength_category(score) == expected
