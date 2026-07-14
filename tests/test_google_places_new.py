"""Places API (New) adapter tests. No network."""
from __future__ import annotations

from app.collectors.google_places import GooglePlacesClient
from app.utils.geo_utils import LatLon, geocode_city


def test_text_search_maps_places_new_to_legacy_shape(monkeypatch):
    client = GooglePlacesClient(api_key="fake", rate_limit_seconds=0)

    def fake_request(method, url, *, field_mask, json_body=None, params=None):
        assert method == "POST"
        assert json_body["textQuery"] == "CPR training"
        assert "places.id" in field_mask
        return {
            "places": [{
                "id": "ChIJ_test",
                "displayName": {"text": "Acme CPR"},
                "formattedAddress": "1 Test Way, San Jose, CA",
                "location": {"latitude": 37.33, "longitude": -121.89},
                "rating": 4.7,
                "userRatingCount": 102,
                "types": ["health", "point_of_interest"],
                "googleMapsUri": "https://maps.google.com/?cid=123",
                "businessStatus": "OPERATIONAL",
                "priceLevel": "PRICE_LEVEL_MODERATE",
                "photos": [{
                    "name": "places/ChIJ_test/photos/photo_abc",
                    "widthPx": 800,
                    "heightPx": 600,
                }],
            }]
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    results = client._text_search_live("CPR training", location=(37.3, -121.9),
                                       radius_meters=5000)

    assert results == [{
        "place_id": "ChIJ_test",
        "name": "Acme CPR",
        "formatted_address": "1 Test Way, San Jose, CA",
        "geometry": {"location": {"lat": 37.33, "lng": -121.89}},
        "rating": 4.7,
        "user_ratings_total": 102,
        "types": ["health", "point_of_interest"],
        "url": "https://maps.google.com/?cid=123",
        "website": "",
        "business_status": "OPERATIONAL",
        "international_phone_number": "",
        "formatted_phone_number": "",
        "opening_hours": {"weekday_text": []},
        "price_level": 2,
        "photos": [{
            "photo_reference": "places/ChIJ_test/photos/photo_abc",
            "width": 800,
            "height": 600,
            "html_attributions": [],
        }],
        "reviews": [],
    }]


def test_place_details_maps_reviews_and_hours(monkeypatch):
    client = GooglePlacesClient(api_key="fake", rate_limit_seconds=0)

    def fake_request(method, url, *, field_mask, json_body=None, params=None):
        assert method == "GET"
        assert url.endswith("/places/ChIJ_test")
        assert "reviews" in field_mask
        return {
            "id": "ChIJ_test",
            "displayName": {"text": "Acme CPR"},
            "regularOpeningHours": {
                "weekdayDescriptions": ["Monday: 9:00 AM - 5:00 PM"],
            },
            "internationalPhoneNumber": "+1 408-555-0100",
            "websiteUri": "https://example.com",
            "reviews": [{
                "text": {"text": "Hard to park."},
                "rating": 2,
                "relativePublishTimeDescription": "a month ago",
            }],
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    details = client._place_details_live("ChIJ_test")

    assert details["place_id"] == "ChIJ_test"
    assert details["opening_hours"]["weekday_text"] == ["Monday: 9:00 AM - 5:00 PM"]
    assert details["international_phone_number"] == "+1 408-555-0100"
    assert details["website"] == "https://example.com"
    assert details["reviews"] == [{
        "text": "Hard to park.",
        "rating": 2,
        "relative_time_description": "a month ago",
        "time": "",
    }]


def test_geocode_city_falls_back_to_places_text(monkeypatch):
    class FakeGeocodeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "REQUEST_DENIED", "results": []}

    class FakePlacesResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "places": [{
                    "id": "ChIJ_sj",
                    "displayName": {"text": "San Jose"},
                    "location": {"latitude": 37.3382, "longitude": -121.8863},
                }]
            }

    monkeypatch.setattr("app.utils.geo_utils.GOOGLE_MAPS_API_KEY", "fake")
    monkeypatch.setattr("app.utils.geo_utils.requests.get",
                        lambda *args, **kwargs: FakeGeocodeResponse())
    monkeypatch.setattr("app.utils.geo_utils.requests.post",
                        lambda *args, **kwargs: FakePlacesResponse())

    assert geocode_city("San Jose", "CA") == LatLon(37.3382, -121.8863)
