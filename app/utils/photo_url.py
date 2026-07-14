"""
Google Places Photo URL builder.

Two modes:
  - internal (key_safe=False): builds a fully-resolvable URL containing the
    API key. Suitable only for reports never shared outside the org.
  - public  (key_safe=True):   returns an empty string (no URL exposed).
    The photo_reference is still stored in JSON for later resolution by a
    proxy that injects the key server-side.

We never embed the key in any file that may be shipped to a customer.
"""
from __future__ import annotations

from typing import Optional

from app.config import GOOGLE_MAPS_API_KEY

PHOTO_ENDPOINT = "https://maps.googleapis.com/maps/api/place/photo"
PLACES_NEW_PHOTO_ENDPOINT = "https://places.googleapis.com/v1"


def build_photo_url(
    photo_reference: str,
    max_width: int = 600,
    api_key: Optional[str] = None,
    key_safe: bool = True,
) -> str:
    """Return a Google Places Photo API URL, or '' if key-safe mode hides it."""
    if not photo_reference:
        return ""
    if key_safe:
        return ""
    key = api_key or GOOGLE_MAPS_API_KEY
    if not key:
        return ""
    if photo_reference.startswith("places/"):
        return (f"{PLACES_NEW_PHOTO_ENDPOINT}/{photo_reference}/media"
                f"?maxWidthPx={int(max_width)}&key={key}")
    return (f"{PHOTO_ENDPOINT}?maxwidth={int(max_width)}"
            f"&photoreference={photo_reference}&key={key}")
