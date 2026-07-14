"""
Normalized place model used by collectors, enrichers, and reports.

Every place (demand driver, competitor, candidate anchor) flows through
`PlaceProfile`. Missing fields stay missing — never imputed.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from app.utils.source_tracker import utcnow_iso


@dataclass
class PhotoMeta:
    """Google Places photo reference + dimensions. URL is built on demand."""
    photo_reference: str
    width: Optional[int] = None
    height: Optional[int] = None
    attributions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "photo_reference": self.photo_reference,
            "width": self.width,
            "height": self.height,
            "attributions": list(self.attributions),
        }


@dataclass
class PlaceProfile:
    """Normalized place row. Any field may be None / empty if not collected."""
    place_id: str = ""
    name: str = ""
    formatted_address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    category: str = ""
    source_query: str = ""
    google_maps_url: str = ""
    website: str = ""
    phone_number: str = ""
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    business_status: str = ""
    types: List[str] = field(default_factory=list)
    opening_hours_weekday_text: List[str] = field(default_factory=list)
    price_level: Optional[int] = None
    distance_miles: Optional[float] = None
    photos: List[PhotoMeta] = field(default_factory=list)
    website_analysis: Dict[str, Any] = field(default_factory=dict)
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    source_api: str = "Google Places API"
    collected_at: str = field(default_factory=utcnow_iso)
    confidence: str = "api"  # "official" | "api" | "inferred" | "estimated" | "unknown"

    # --- Construction helpers --------------------------------------------- #

    @classmethod
    def from_google_places(
        cls,
        raw: Dict[str, Any],
        category: str,
        origin: Optional[Tuple[float, float]] = None,
        source_query: str = "",
    ) -> "PlaceProfile":
        """Build a PlaceProfile from a Nearby/Text Search result dict.

        Does NOT call Place Details — phone/website/hours stay empty unless the
        raw dict already has them (text search sometimes does).
        """
        loc = (raw.get("geometry") or {}).get("location") or {}
        lat = loc.get("lat")
        lon = loc.get("lng")

        distance: Optional[float] = None
        if origin is not None and lat is not None and lon is not None:
            from app.utils.geo_utils import haversine_miles
            distance = round(haversine_miles(origin, (lat, lon)), 3)

        photos: List[PhotoMeta] = []
        for ph in raw.get("photos") or []:
            ref = ph.get("photo_reference")
            if not ref:
                continue
            photos.append(PhotoMeta(
                photo_reference=ref,
                width=ph.get("width"),
                height=ph.get("height"),
                attributions=list(ph.get("html_attributions") or []),
            ))

        hours: List[str] = []
        oh = raw.get("opening_hours") or {}
        if isinstance(oh, dict):
            hours = list(oh.get("weekday_text") or [])

        return cls(
            place_id=raw.get("place_id", "") or "",
            name=raw.get("name", "") or "",
            formatted_address=(raw.get("formatted_address")
                               or raw.get("vicinity") or ""),
            latitude=lat,
            longitude=lon,
            category=category,
            source_query=source_query,
            google_maps_url=raw.get("url", "") or "",
            website=raw.get("website", "") or "",
            phone_number=(raw.get("international_phone_number")
                          or raw.get("formatted_phone_number") or ""),
            rating=raw.get("rating"),
            user_ratings_total=raw.get("user_ratings_total"),
            business_status=raw.get("business_status", "") or "",
            types=list(raw.get("types") or []),
            opening_hours_weekday_text=hours,
            price_level=raw.get("price_level"),
            distance_miles=distance,
            photos=photos,
        )

    def merge_details(self, details: Dict[str, Any]) -> None:
        """Fold a Place Details response onto an existing profile."""
        if not details:
            return
        if not self.website and details.get("website"):
            self.website = details["website"]
        phone = (details.get("international_phone_number")
                 or details.get("formatted_phone_number"))
        if phone and not self.phone_number:
            self.phone_number = phone
        if not self.google_maps_url and details.get("url"):
            self.google_maps_url = details["url"]
        oh = details.get("opening_hours") or {}
        if isinstance(oh, dict) and oh.get("weekday_text"):
            self.opening_hours_weekday_text = list(oh["weekday_text"])
        if details.get("price_level") is not None:
            self.price_level = details["price_level"]
        for ph in details.get("photos") or []:
            ref = ph.get("photo_reference")
            if ref and not any(p.photo_reference == ref for p in self.photos):
                self.photos.append(PhotoMeta(
                    photo_reference=ref,
                    width=ph.get("width"),
                    height=ph.get("height"),
                    attributions=list(ph.get("html_attributions") or []),
                ))
        if not self.business_status and details.get("business_status"):
            self.business_status = details["business_status"]
        # Google Place Details returns up to 5 reviews (text + rating).
        # Normalize to the {text, rating, time_created} shape the
        # complaint-theme engine expects.
        if details.get("reviews") and not self.reviews:
            normalized = []
            for rv in details["reviews"]:
                if not isinstance(rv, dict):
                    continue
                normalized.append({
                    "text": rv.get("text") or "",
                    "rating": rv.get("rating"),
                    "time_created": rv.get("relative_time_description")
                    or str(rv.get("time") or ""),
                })
            self.reviews = normalized

    # --- Convenience -------------------------------------------------------- #

    @property
    def has_photo(self) -> bool:
        return bool(self.photos)

    @property
    def maps_url_fallback(self) -> str:
        """Always-resolvable Google Maps link, using place_id if no direct url."""
        if self.google_maps_url:
            return self.google_maps_url
        if self.place_id:
            return f"https://www.google.com/maps/place/?q=place_id:{self.place_id}"
        if self.latitude is not None and self.longitude is not None:
            return f"https://www.google.com/maps?q={self.latitude},{self.longitude}"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["photos"] = [p.to_dict() for p in self.photos]
        return d


def unknown_if_none(value: Any) -> Any:
    """Stringify Nones as 'unknown' for tabular output. Never invent."""
    if value is None or value == "":
        return "unknown"
    return value
