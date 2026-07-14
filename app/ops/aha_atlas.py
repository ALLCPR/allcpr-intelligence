"""
AHA Atlas live instructor-lead source (Training Centers / Training Sites).

Ports a coworker's reverse-engineered AHA Atlas client into the ops layer so a
ZIP search can pull **real, contactable AHA training centers** (phone + email +
address + the AHA disciplines they teach) and feed them straight into the same
instructor pipeline everything else uses: store → match → outreach draft →
approval queue.

Why these are good leads: the Atlas API returns AHA-registered Training Centers
(``TC``) and Training Sites (``TS``). We keep the ones with **no website of
their own** — small operators / individual instructors who are the realistic
recruiting targets, exactly the coworker tool's filter — and drop institutional
``.org``/``.gov`` contacts. Each becomes a Level-2 "Named Lead"
(``credential_status = NEEDS_VERIFICATION``): a named, contactable organization
whose current AHA cert we still re-verify before it counts as VERIFIED. The
credential-honesty rule (discovery never emits VERIFIED) is preserved.

This is the one genuinely-live ops source (a network call to AHA's gateway). It
is env-gated (``AHA_ATLAS_ENABLED``) and memoized with a short TTL so repeated
ZIP clicks don't hammer the upstream. CORS blocks browser calls, so it must run
server-side — which is why it lives here and not in the dashboard JS.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

from app.config import REQUEST_TIMEOUT
from app.ops.models import AHA_BLS, InstructorCandidate
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# ============================================================
# Guest token for AHA's public Atlas API — supplied via env, never committed.
# AHA serves a guest token publicly from its frontend bundle (_app-*.js); set
# AHA_GUEST_TOKEN to that value to enable this feature. Unset = the AHA lookup
# reports itself unavailable and no network call is made.
# ============================================================
GUEST_TOKEN = os.environ.get("AHA_GUEST_TOKEN", "")

API_BASE = os.environ.get(
    "AHA_ATLAS_API_BASE",
    "https://atlas-api-gateway.heart.org/orgManagement/v1/orgSearch",
)

# Off switch (defaults on). Turn off to disable all network calls to AHA — the
# route then reports the feature as disabled instead of reaching out.
AHA_ATLAS_ENABLED = os.environ.get("AHA_ATLAS_ENABLED", "true").lower() != "false"

# Placeholder phone AHA stores when a center has no real number.
_PLACEHOLDER_PHONE = "0000000000"

# The AHA API takes radius in meters. Curated conversions match the Atlas UI's
# radius buttons; anything else is converted at ~1609 m/mi.
_MILES_TO_METERS = {10: 16093, 25: 40234, 50: 80467, 75: 120701, 100: 160934}

# Short-lived in-process cache: {(zip, radius): (fetched_at, centers)}.
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = int(os.environ.get("AHA_ATLAS_CACHE_TTL", "900"))  # 15 min


def _radius_meters(radius_miles: float) -> int:
    return _MILES_TO_METERS.get(int(radius_miles), int(round(radius_miles * 1609)))


def _fetch_page(location: str, radius_meters: int, size: int = 500,
                page: int = 1) -> List[Dict[str, Any]]:
    """One AHA Atlas orgSearch page. Raises on HTTP error."""
    params = {
        "size": size,
        "status": "ACTIVE",
        "location": location,
        "locationMainText": location,
        "locationTypes": "country",
        "enforceRadius": "true",
        "radius": radius_meters,
        "applyCountry": "true",
        "page": page,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Jwt-Token": GUEST_TOKEN,
        "Origin": "https://atlas.heart.org",
        "Referer": "https://atlas.heart.org/",
    }
    resp = requests.get(API_BASE, params=params, headers=headers,
                        timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json() or {}
    return (data.get("data") or {}).get("items") or []


def fetch_training_centers(zip_code: str, radius_miles: float = 25.0,
                           ) -> List[Dict[str, Any]]:
    """Raw AHA Atlas items near a ZIP (the ZIP itself is the geocoder input).

    A single size=500 page covers any realistic city+radius; AHA's totalCount
    is unreliable for guest tokens so we do not chase pagination.
    """
    zip_code = str(zip_code).zfill(5)
    return _fetch_page(zip_code, _radius_meters(radius_miles))


def extract_center(item: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one raw Atlas item into the fields we care about."""
    profile = item.get("organisationProfile") or {}
    coordinator = profile.get("coordinator") or {}

    street = ", ".join(
        p for p in (profile.get("address1") or "", profile.get("address2") or "",
                    profile.get("address3") or "") if p)
    city = profile.get("city") or ""
    state = profile.get("state") or ""
    postal = profile.get("postalCode") or ""
    country = profile.get("country") or ""
    city_state_zip = ", ".join(p for p in (city, state) if p)
    if postal:
        city_state_zip = f"{city_state_zip} {postal}".strip()
    full_address = ", ".join(p for p in (street, city_state_zip, country) if p)

    email = (item.get("email") or coordinator.get("email") or "").strip()

    phone = str(item.get("phone") or "").strip()
    if phone.replace("-", "").replace(" ", "") == _PLACEHOLDER_PHONE:
        phone = ""

    distance_m = item.get("distance")
    distance_miles = (round(distance_m * 0.000621371, 1)
                      if isinstance(distance_m, (int, float)) else None)

    disciplines = item.get("disciplines") or []
    disc_names = [d.get("name", "") for d in disciplines if d.get("name")]
    disc_codes = [d.get("code", "") for d in disciplines if d.get("code")]

    return {
        "name": (item.get("name") or "").strip(),
        "code": item.get("code") or "",
        "type": item.get("organisationType") or "",  # TC or TS
        "phone": phone,
        "email": email,
        "website": (item.get("websiteUrl") or "").strip(),
        "street": street,
        "city": city,
        "state": state,
        "postal": postal,
        "full_address": full_address,
        "latitude": profile.get("latitude"),
        "longitude": profile.get("longitude"),
        "distance_miles": distance_miles,
        "disciplines": disc_names,
        "discipline_codes": disc_codes,
    }


def _is_recruiting_lead(center: Dict[str, Any], radius_miles: float) -> bool:
    """Keep only realistic recruiting targets (matches the coworker tool)."""
    if center.get("website"):
        return False  # established org with its own web presence — skip
    email = (center.get("email") or "").lower()
    if email.endswith((".org", ".gov")):
        return False  # institutional / government contact — skip
    if not (center.get("email") or center.get("phone")):
        return False  # nothing to reach out on
    dist = center.get("distance_miles")
    if dist is not None and dist > radius_miles:
        return False
    return True


def _center_to_candidate(zip_code: str, center: Dict[str, Any]
                         ) -> Dict[str, Any]:
    """Map one AHA center to an InstructorCandidate dict (Named Lead)."""
    signals = [f"AHA {center.get('type') or 'center'} {center.get('code')}".strip()]
    signals += [c for c in (center.get("discipline_codes") or []) if c]

    notes = (
        f"AHA Atlas {('Training Center' if center.get('type') == 'TC' else 'Training Site')} "
        f"{center.get('code') or ''}".strip()
    )
    if center.get("disciplines"):
        notes += " · Teaches: " + ", ".join(center["disciplines"])
    notes += " · Live AHA lead — verify current instructor certification before scheduling."

    # Stable id from the AHA org code so re-fetching a ZIP merges the same
    # center (preserving any CRM state) instead of creating a duplicate.
    code = str(center.get("code") or "").strip()
    stable_id = f"aha_{code}" if code else None

    kwargs = dict(
        name=center.get("name") or "AHA Training Site",
        source="aha_atlas",
        source_url="https://atlas.heart.org/training-center-search",
        email=center.get("email") or "",
        phone=center.get("phone") or "",
        organization=center.get("name") or "",
        title=("AHA Training Center" if center.get("type") == "TC"
               else "AHA Training Site"),
        city=center.get("city") or "",
        state=center.get("state") or "",
        zip=center.get("postal") or "",
        lat=center.get("latitude"),
        lng=center.get("longitude"),
        distance_miles=center.get("distance_miles"),
        candidate_type="AHA_BLS_INSTRUCTOR",
        certification_signals=signals,
        # Real AHA registration is a strong signal but NOT staff-verified.
        credential_status="NEEDS_VERIFICATION",
        courses_possible=[AHA_BLS],
        long_term_interest="UNKNOWN",
        notes=notes,
    )
    if stable_id:
        kwargs["id"] = stable_id
    return InstructorCandidate(**kwargs).to_dict()


def aha_instructor_candidates(zip_code: str, radius_miles: float = 25.0,
                              limit: int = 25,
                              use_cache: bool = True) -> Dict[str, Any]:
    """Live AHA leads near a ZIP, mapped to InstructorCandidate dicts.

    Returns ``{"ok", "zip", "radius_miles", "candidates", "fetched",
    "kept", "note"}``. Never raises — upstream/network failures come back as
    ``ok=False`` with a message so the route degrades gracefully.
    """
    zip_code = str(zip_code).zfill(5)
    if not AHA_ATLAS_ENABLED:
        return {"ok": False, "zip": zip_code, "radius_miles": radius_miles,
                "candidates": [], "fetched": 0, "kept": 0,
                "note": "AHA Atlas source is disabled (AHA_ATLAS_ENABLED=false)."}
    if not GUEST_TOKEN:
        return {"ok": False, "zip": zip_code, "radius_miles": radius_miles,
                "candidates": [], "fetched": 0, "kept": 0,
                "note": ("AHA Atlas lookup needs AHA_GUEST_TOKEN in the "
                         "environment; no token is set, so no request was "
                         "made.")}

    cache_key = f"{zip_code}:{int(radius_miles)}"
    now = time.time()
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
            centers = hit[1]
            return _build_result(zip_code, radius_miles, centers, limit,
                                 cached=True)

    try:
        raw = fetch_training_centers(zip_code, radius_miles)
    except requests.RequestException as exc:
        logger.warning(f"aha_atlas: fetch failed for {zip_code}: {exc}")
        return {"ok": False, "zip": zip_code, "radius_miles": radius_miles,
                "candidates": [], "fetched": 0, "kept": 0,
                "note": f"AHA Atlas request failed: {str(exc)[:200]}"}
    except (ValueError, KeyError) as exc:  # bad JSON / shape change
        logger.warning(f"aha_atlas: bad response for {zip_code}: {exc}")
        return {"ok": False, "zip": zip_code, "radius_miles": radius_miles,
                "candidates": [], "fetched": 0, "kept": 0,
                "note": "AHA Atlas returned an unexpected response."}

    centers = [extract_center(item) for item in raw]
    _CACHE[cache_key] = (now, centers)
    return _build_result(zip_code, radius_miles, centers, limit, cached=False)


def _build_result(zip_code: str, radius_miles: float,
                  centers: List[Dict[str, Any]], limit: int,
                  cached: bool) -> Dict[str, Any]:
    kept = [c for c in centers if _is_recruiting_lead(c, radius_miles)]
    kept.sort(key=lambda c: (c.get("distance_miles") is None,
                             c.get("distance_miles") or 9999.0))
    candidates = [_center_to_candidate(zip_code, c) for c in kept[:max(1, limit)]]
    note = (f"{len(kept)} contactable AHA lead(s) within {int(radius_miles)} mi "
            f"of {zip_code} (of {len(centers)} centers).")
    if cached:
        note += " (cached)"
    return {
        "ok": True,
        "zip": zip_code,
        "radius_miles": radius_miles,
        "candidates": candidates,
        "fetched": len(centers),
        "kept": len(kept),
        "note": note,
    }
