"""Yelp competitor augmentation tests.

Covers:
- feature-flag fallback when YELP_API_KEY is unset
- response → normalized record shape
- match logic: name jaccard + 0.05mi proximity
- augmentation mutates Google PlaceProfile with yelp_augmentation
- Yelp-only competitors counted but kept distinct
- competition_summary picks up yelp_* counts
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import yelp_competitors  # noqa: E402


# --------------------------------------------------------------------------- #
# Feature-flag fallback
# --------------------------------------------------------------------------- #

def test_yelp_not_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(yelp_competitors, "YELP_API_KEY", "")
    assert yelp_competitors.is_configured() is False
    out = yelp_competitors.fetch_yelp_competitors(
        origin=(37.7749, -122.4194), radius_miles=2.0,
    )
    assert out == []


# --------------------------------------------------------------------------- #
# Response normalization
# --------------------------------------------------------------------------- #

def _fake_yelp_business(
    *, name: str, yelp_id: str = "biz1", lat: float = 37.78, lon: float = -122.42,
    rating: float = 4.5, reviews: int = 120,
    categories: list = None,
) -> dict:
    return {
        "id": yelp_id,
        "name": name,
        "url": f"https://yelp.com/biz/{yelp_id}",
        "rating": rating,
        "review_count": reviews,
        "categories": [
            {"alias": "cprclasses", "title": "CPR Classes"},
        ] if categories is None else categories,
        "phone": "+14155551212",
        "price": "$$",
        "coordinates": {"latitude": lat, "longitude": lon},
    }


def test_yelp_response_normalizes_to_dict(monkeypatch):
    monkeypatch.setattr(yelp_competitors, "YELP_API_KEY", "test-bearer")
    fake = [_fake_yelp_business(name="Acme CPR Training")]
    with patch.object(yelp_competitors, "_yelp_request", return_value=fake):
        out = yelp_competitors.fetch_yelp_competitors(
            origin=(37.7749, -122.4194), radius_miles=2.0,
        )
    assert len(out) == 1
    rec = out[0]
    assert rec["name"] == "Acme CPR Training"
    assert rec["yelp_id"] == "biz1"
    assert rec["yelp_rating"] == 4.5
    assert rec["yelp_review_count"] == 120
    assert "CPR Classes" in rec["yelp_categories"]


# --------------------------------------------------------------------------- #
# Match + augmentation
# --------------------------------------------------------------------------- #

def _google_competitor(name: str, lat: float, lon: float):
    """Mimic a PlaceProfile-shaped object for matching tests."""
    return SimpleNamespace(name=name, latitude=lat, longitude=lon)


def test_match_jaccard_and_proximity_succeeds():
    google = _google_competitor("Acme CPR Training Center", 37.780, -122.420)
    yelp_norm = yelp_competitors._normalize_business(_fake_yelp_business(
        name="Acme CPR Training",  # close name
        lat=37.78005, lon=-122.42010,  # within 80m
    ))
    assert yelp_competitors._match_yelp_to_google(yelp_norm, google) is True


def test_match_fails_when_too_far():
    google = _google_competitor("Acme CPR Training Center", 37.780, -122.420)
    yelp_norm = yelp_competitors._normalize_business(_fake_yelp_business(
        name="Acme CPR Training",
        lat=37.800, lon=-122.450,  # several miles away
    ))
    assert yelp_competitors._match_yelp_to_google(yelp_norm, google) is False


def test_match_fails_when_names_differ():
    google = _google_competitor("Acme CPR Training Center", 37.780, -122.420)
    yelp_norm = yelp_competitors._normalize_business(_fake_yelp_business(
        name="Totally Unrelated Bakery",
        lat=37.78005, lon=-122.42010,  # at same coords
    ))
    assert yelp_competitors._match_yelp_to_google(yelp_norm, google) is False


def test_augment_mutates_matched_google_competitor():
    google = _google_competitor("SF Health & Safety Training", 37.780, -122.420)
    yelp_rec = {
        "yelp_id": "y1",
        "name": "SF Health Safety Training",
        "yelp_url": "https://yelp.com/biz/y1",
        "yelp_rating": 4.7,
        "yelp_review_count": 300,
        "yelp_categories": ["CPR Classes", "First Aid Classes"],
        "latitude": 37.78002,
        "longitude": -122.42001,
    }
    summary = yelp_competitors.augment_competitors_with_yelp(
        [google], [yelp_rec],
    )
    assert summary["yelp_matched_count"] == 1
    assert summary["yelp_only_count"] == 0
    assert hasattr(google, "yelp_augmentation")
    assert google.yelp_augmentation["yelp_rating"] == 4.7
    assert "CPR Classes" in google.yelp_augmentation["yelp_categories"]


def test_yelp_only_competitor_counted_separately():
    google = _google_competitor("Some Google Place", 37.780, -122.420)
    yelp_rec_match = {
        "yelp_id": "y1",
        "name": "Some Google Place",  # matches
        "yelp_rating": 4.5,
        "yelp_review_count": 100,
        "yelp_categories": [],
        "latitude": 37.78005, "longitude": -122.42010,
    }
    yelp_rec_unmatched = {
        "yelp_id": "y2",
        "name": "Hidden BLS Trainer LLC",
        "yelp_rating": 5.0,
        "yelp_review_count": 25,
        "yelp_categories": [],
        "latitude": 37.795, "longitude": -122.430,
    }
    summary = yelp_competitors.augment_competitors_with_yelp(
        [google], [yelp_rec_match, yelp_rec_unmatched],
    )
    assert summary["yelp_matched_count"] == 1
    assert summary["yelp_only_count"] == 1
    assert len(summary["yelp_unmatched_competitors"]) == 1


def test_yelp_summary_averages_rating_and_sums_reviews():
    g1 = _google_competitor("A", 37.780, -122.420)
    g2 = _google_competitor("B", 37.781, -122.421)
    y1 = {
        "yelp_id": "y1", "name": "A", "yelp_rating": 4.0,
        "yelp_review_count": 50, "yelp_categories": [],
        "latitude": 37.780, "longitude": -122.420,
    }
    y2 = {
        "yelp_id": "y2", "name": "B", "yelp_rating": 5.0,
        "yelp_review_count": 150, "yelp_categories": [],
        "latitude": 37.781, "longitude": -122.421,
    }
    summary = yelp_competitors.augment_competitors_with_yelp([g1, g2], [y1, y2])
    assert summary["yelp_matched_count"] == 2
    assert summary["yelp_avg_rating"] == 4.5
    assert summary["yelp_total_reviews"] == 200


def test_augment_with_no_yelp_records_is_safe_noop():
    google = _google_competitor("Anywhere", 37.780, -122.420)
    summary = yelp_competitors.augment_competitors_with_yelp([google], [])
    assert summary["yelp_matched_count"] == 0
    assert summary["yelp_only_count"] == 0
    assert summary["yelp_avg_rating"] is None
    assert not hasattr(google, "yelp_augmentation")


def test_name_jaccard_empty_inputs_safe():
    assert yelp_competitors._name_jaccard("", "anything") == 0.0
    assert yelp_competitors._name_jaccard("anything", "") == 0.0
    assert yelp_competitors._name_jaccard("", "") == 0.0
