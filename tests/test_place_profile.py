"""Tests for the PlaceProfile model + photo URL safety. No network."""
from __future__ import annotations

from app.models.place_profile import PlaceProfile, PhotoMeta
from app.utils.photo_url import build_photo_url


def _raw():
    return {
        "place_id": "ChIJ_test",
        "name": "Test Hospital",
        "formatted_address": "1 Test Way, Testville, CA 95000",
        "geometry": {"location": {"lat": 37.0, "lng": -121.0}},
        "rating": 4.5,
        "user_ratings_total": 200,
        "types": ["hospital", "health"],
        "url": "https://maps.google.com/?cid=123",
        "photos": [{
            "photo_reference": "ref_abc",
            "width": 800, "height": 600,
            "html_attributions": ["<a>Test</a>"],
        }],
        "opening_hours": {"weekday_text": ["Mon: 9-5"]},
    }


def test_from_google_places_populates_core_fields():
    p = PlaceProfile.from_google_places(_raw(), category="hospital",
                                        origin=(37.0, -121.0),
                                        source_query="hospital")
    assert p.name == "Test Hospital"
    assert p.formatted_address == "1 Test Way, Testville, CA 95000"
    assert p.rating == 4.5
    assert p.user_ratings_total == 200
    assert p.has_photo
    assert p.distance_miles == 0.0  # same lat/lon
    assert "hospital" in p.types


def test_unknown_fields_stay_unknown_not_invented():
    raw = {"place_id": "x", "name": "Mystery", "geometry": {"location": {}}}
    p = PlaceProfile.from_google_places(raw, category="competitor")
    assert p.rating is None
    assert p.user_ratings_total is None
    assert p.phone_number == ""
    assert p.website == ""
    assert p.distance_miles is None


def test_merge_details_does_not_overwrite_present_fields():
    p = PlaceProfile.from_google_places(_raw(), category="hospital")
    p.website = "https://existing.example"
    details = {
        "website": "https://new.example",
        "international_phone_number": "+1-555-555-5555",
    }
    p.merge_details(details)
    assert p.website == "https://existing.example"   # not overwritten
    assert p.phone_number == "+1-555-555-5555"       # filled in


def test_maps_url_fallback_uses_place_id_when_no_url():
    p = PlaceProfile(place_id="abc", latitude=10.0, longitude=20.0)
    url = p.maps_url_fallback
    assert "place_id" in url and "abc" in url


def test_maps_url_fallback_uses_coords_as_last_resort():
    p = PlaceProfile(latitude=10.0, longitude=20.0)
    url = p.maps_url_fallback
    assert "10.0,20.0" in url


def test_photo_url_safe_mode_hides_key():
    url = build_photo_url("ref_abc", api_key="SECRET", key_safe=True)
    assert url == ""


def test_photo_url_unsafe_mode_embeds_key():
    url = build_photo_url("ref_abc", api_key="SECRET", key_safe=False)
    assert "SECRET" in url
    assert "ref_abc" in url


def test_photo_url_supports_places_new_resource_names():
    url = build_photo_url("places/p1/photos/photo1", api_key="SECRET",
                          key_safe=False)
    assert url.startswith("https://places.googleapis.com/v1/places/p1/photos/photo1/media")
    assert "maxWidthPx=600" in url
    assert "SECRET" in url


def test_photo_url_no_reference_returns_empty():
    assert build_photo_url("", key_safe=False, api_key="K") == ""
