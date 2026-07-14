"""Review complaint-theme detection tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.enrichers.review_sentiment import (  # noqa: E402
    NEGATIVE_RATING_MAX,
    analyze_reviews,
    detect_themes_in_text,
    summarize_positioning,
)


# --------------------------------------------------------------------------- #
# Theme detection
# --------------------------------------------------------------------------- #

def test_detects_parking_complaint():
    assert "parking" in detect_themes_in_text(
        "There was nowhere to park, parking was a nightmare.")


def test_detects_scheduling_complaint():
    assert "scheduling" in detect_themes_in_text(
        "They cancelled my class twice and there was no availability.")


def test_detects_refund_complaint():
    assert "refund" in detect_themes_in_text(
        "They refused to refund me after they cancelled.")


def test_detects_instructor_complaint():
    assert "instructor_quality" in detect_themes_in_text(
        "The instructor was rude and completely unprofessional.")


def test_clean_text_has_no_themes():
    assert detect_themes_in_text("Great class, learned a lot, highly recommend!") == []


# --------------------------------------------------------------------------- #
# Aggregation — negative reviews only
# --------------------------------------------------------------------------- #

def test_positive_review_mentioning_parking_not_counted():
    reviews = [
        {"text": "Parking was easy and the class was great!", "rating": 5},
    ]
    out = analyze_reviews(reviews)
    # 5-star review is skipped → no parking complaint
    assert out.theme_counts.get("parking", 0) == 0


def test_negative_reviews_aggregate_themes():
    reviews = [
        {"text": "No parking anywhere, terrible.", "rating": 1},
        {"text": "Parking was impossible and they cancelled on me.", "rating": 2},
        {"text": "Instructor was rude.", "rating": 2},
    ]
    out = analyze_reviews(reviews)
    assert out.theme_counts["parking"] == 2
    assert out.theme_counts["scheduling"] == 1
    assert out.theme_counts["instructor_quality"] == 1
    assert out.negative_reviews == 3


def test_top_frustrations_sorted_by_count():
    reviews = [
        {"text": "No parking.", "rating": 1},
        {"text": "Parking nightmare.", "rating": 1},
        {"text": "Parking was impossible.", "rating": 2},
        {"text": "They cancelled my class.", "rating": 2},
    ]
    out = analyze_reviews(reviews)
    assert out.top_frustrations[0]["theme"] == "parking"
    assert out.top_frustrations[0]["count"] == 3
    # opportunity hint present
    assert out.top_frustrations[0]["opportunity"]


def test_rating_threshold_boundary():
    # rating exactly at the negative max is included
    reviews = [{"text": "No parking at all.",
                "rating": NEGATIVE_RATING_MAX}]
    out = analyze_reviews(reviews)
    assert out.theme_counts.get("parking", 0) == 1


def test_data_confidence_scales_with_volume():
    one = analyze_reviews([{"text": "x", "rating": 1}])
    assert one.data_confidence == "low"
    many = analyze_reviews([{"text": "x", "rating": 1}] * 12)
    assert many.data_confidence == "high"


def test_empty_reviews_safe():
    out = analyze_reviews([])
    assert out.reviews_scanned == 0
    assert out.top_frustrations == []
    assert out.data_confidence == "low"


# --------------------------------------------------------------------------- #
# Positioning summary
# --------------------------------------------------------------------------- #

def test_positioning_names_top_themes():
    reviews = [
        {"text": "No parking, parking nightmare.", "rating": 1},
        {"text": "Parking impossible.", "rating": 1},
        {"text": "They cancelled twice.", "rating": 2},
    ]
    out = analyze_reviews(reviews)
    msg = summarize_positioning(out)
    assert "parking" in msg.lower()
    assert "lead with the opposite" in msg.lower()


def test_positioning_handles_no_reviews():
    out = analyze_reviews([])
    msg = summarize_positioning(out)
    assert "no competitor reviews" in msg.lower()


def test_positioning_handles_clean_reviews():
    reviews = [{"text": "Loved it!", "rating": 1}]  # negative but no theme
    out = analyze_reviews(reviews)
    msg = summarize_positioning(out)
    assert "no recurring complaints" in msg.lower()
