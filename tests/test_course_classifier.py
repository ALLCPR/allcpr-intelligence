"""
Tests for the deterministic course-type classifier (STEP 3).

The classifier is the single source of truth for ALLCPR's course taxonomy.
These cover the canonical buckets, the new ACLS/PALS rules, the no-guess
fallback, and that ``app.collectors.enrollware`` re-exports the same objects
(so existing imports keep working after the extraction).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.enrichers.course_classifier import (  # noqa: E402
    COURSE_TYPE_LABELS,
    classify_course_type,
)


@pytest.mark.parametrize("name,expected", [
    # Provider + course buckets.
    ("AHA BLS Provider", "aha_bls"),
    ("AHA Heartsaver CPR AED", "aha_cpr"),
    ("Red Cross Basic Life Support", "arc_bls"),
    ("Red Cross Adult CPR/AED", "arc_cpr"),
    ("ALLCPR BLS Provider Course", "allcpr_bls"),
    # Generic blended (no provider).
    ("Adult First Aid/CPR/AED", "cpr_first_aid_blended"),
    # Skills sessions win over everything.
    ("AHA BLS Skills Session", "skills_session"),
    ("RQI Skills", "skills_session"),
    # Company-name "AllCPR" substring must not register a CPR course.
    ("AllCPR Group Training", "unknown_course_type"),
    # No-guess fallback.
    ("Some Random Lifeguard Class", "unknown_course_type"),
    ("", "unknown_course_type"),
    (None, "unknown_course_type"),
])
def test_known_buckets(name, expected):
    assert classify_course_type(name) == expected


@pytest.mark.parametrize("name", [
    "ACLS Provider",
    "AHA ACLS Provider Course (Initial & Renewal)",
    "Advanced Cardiovascular Life Support",
    "ACLS Recert",
])
def test_acls_detected(name):
    assert classify_course_type(name) == "acls"


@pytest.mark.parametrize("name", [
    "PALS Provider",
    "AHA PALS Renewal",
    "Pediatric Advanced Life Support",
])
def test_pals_detected(name):
    assert classify_course_type(name) == "pals"


def test_acls_pals_in_catalog():
    assert COURSE_TYPE_LABELS["acls"] == "ACLS"
    assert COURSE_TYPE_LABELS["pals"] == "PALS"


def test_acls_beats_provider_and_bls_absence():
    # "AHA ACLS" has no BLS/CPR token; without the ACLS rule it would fall to
    # unknown. The dedicated rule rescues it into the ACLS bucket.
    assert classify_course_type("AHA ACLS") == "acls"


def test_skills_still_beats_acls():
    # A skills/remediation session is its own bucket regardless of course.
    assert classify_course_type("ACLS Skills Session") == "skills_session"


def test_word_boundary_avoids_false_positive():
    # "pals" / "acls" must be whole words, not substrings of other words.
    assert classify_course_type("Principals Leadership CPR Day") != "pals"


def test_enrollware_reexports_same_objects():
    from app.collectors import enrollware
    assert enrollware.classify_course_type is classify_course_type
    assert enrollware.COURSE_TYPE_LABELS is COURSE_TYPE_LABELS
