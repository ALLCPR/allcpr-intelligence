"""
Anchor status classification — separate *area opportunity* from *site validation*.

A grid candidate is anchored to the nearest real-world place so the report can
name the area. But a nearby supermarket or news office is only a *proxy* for the
area; presenting it as the candidate name makes a landmark look like a
lease-ready site. This module grades the anchor by what kind of place it is:

  Tier 1  →  ``verified_commercial_site``  (coworking, office / medical-office
             building, training center, commercial suite, college, community
             center) — a real leasable commercial building *type*.
  Tier 2  →  ``commercial_plaza``          (shopping plaza, business park, large
             retail / commercial center) — commercial area, no specific suite.
  Tier 3  →  ``area_proxy``                (supermarket, single retail store,
             restaurant, news office, random business) — landmark proxy only.
  Tier 4  →  ``invalid_anchor``            (transit stop, parking lot,
             intersection, residential address, government office, airport,
             industrial yard) — not an anchor at all.

It is a *labeling* layer only: it never unlocks ``site_score`` (a real lease
still requires the validated commercial-listing override) and changes no scoring
math. ``site_score`` stays withheld for ``area_proxy`` / ``invalid_anchor``; the
area score is always fine to show.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.utils.viability_filter import is_anchor_viable

# Stable status keys.
VERIFIED_COMMERCIAL_SITE = "verified_commercial_site"
COMMERCIAL_PLAZA = "commercial_plaza"
AREA_PROXY = "area_proxy"
INVALID_ANCHOR = "invalid_anchor"

STATUS_LABELS: Dict[str, str] = {
    VERIFIED_COMMERCIAL_SITE: "Verified commercial site",
    COMMERCIAL_PLAZA: "Commercial plaza",
    AREA_PROXY: "Area proxy only",
    INVALID_ANCHOR: "Invalid anchor",
}

# Representative anchor-quality score (0–100) per status. Deterministic.
STATUS_QUALITY: Dict[str, int] = {
    VERIFIED_COMMERCIAL_SITE: 90,
    COMMERCIAL_PLAZA: 70,
    AREA_PROXY: 45,
    INVALID_ANCHOR: 8,
}

# Site_score may only ever be shown for these statuses (and still only when a
# real validation override exists). The others always withhold it.
_SITE_SCORE_WITHHELD = frozenset({AREA_PROXY, INVALID_ANCHOR})

# Tier 1 — leasable commercial building TYPES (Google Places types).
_TIER1_TYPES: frozenset = frozenset({
    "university", "college", "school", "primary_school", "secondary_school",
    "doctor", "dentist", "physiotherapist", "medical_lab", "dental_clinic",
})
# Tier 1 — name fragments (most specific; checked before Tier 2 generics).
_TIER1_NAME_HINTS: Tuple[str, ...] = (
    "coworking", "co-working", "wework", "regus", "executive suite",
    "executive suites", "commercial suite", "office suite",
    "medical office building", "medical office", "medical building",
    "medical plaza", "medical center", "medical arts",
    "office building", "office tower", "office park", "office plaza",
    "professional building", "professional center", "professional plaza",
    "training center", "training centre", "learning center", "education center",
    "career center", "conference center", "community center", "community centre",
    "college", "university", "campus",
)

# Tier 2 — commercial plaza / retail-center TYPES + name fragments.
_TIER2_TYPES: frozenset = frozenset({
    "shopping_mall",
})
_TIER2_NAME_HINTS: Tuple[str, ...] = (
    "shopping plaza", "shopping center", "shopping centre", "shopping mall",
    "retail center", "retail plaza", "retail park", "power center",
    "business park", "business center", "business plaza", "commerce center",
    "commercial center", "commercial plaza", "commercial park",
    "town center", "town centre", "marketplace", "market place",
    "galleria", "outlets", "outlet center", "plaza", "mall", "centre",
)


@dataclass
class AnchorAssessment:
    anchor_status: str
    anchor_status_label: str
    anchor_quality_score: int
    anchor_display_name: str
    site_score_withheld: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


def classify_anchor(
    types: Iterable[str] = (),
    name: str = "",
    formatted_address: str = "",
) -> AnchorAssessment:
    """Grade an anchor into one of the four tiers. Pure + deterministic."""
    clean = _clean_name(name)
    type_set = {str(t).lower() for t in (types or [])}
    name_lower = clean.lower()

    # Tier 4 first: anything the viability filter rejects (transit, parking,
    # intersection, government, industrial, residential premise) is invalid.
    viable, reason = is_anchor_viable(
        types=types, name=name, formatted_address=formatted_address,
    )
    if not viable:
        return _make(INVALID_ANCHOR, clean or "Unnamed location", reason)

    # Tier 1: real commercial-building type or a specific office/medical/
    # training/college name fragment.
    if type_set & _TIER1_TYPES:
        return _make(VERIFIED_COMMERCIAL_SITE, clean,
                     f"commercial building type: {sorted(type_set & _TIER1_TYPES)[0]}")
    hit = _first_hint(name_lower, _TIER1_NAME_HINTS)
    if hit:
        return _make(VERIFIED_COMMERCIAL_SITE, clean, f"commercial site name: {hit}")

    # Tier 2: shopping plaza / business park / large retail or commercial center.
    if type_set & _TIER2_TYPES:
        return _make(COMMERCIAL_PLAZA, clean,
                     f"plaza/retail type: {sorted(type_set & _TIER2_TYPES)[0]}")
    hit = _first_hint(name_lower, _TIER2_NAME_HINTS)
    if hit:
        return _make(COMMERCIAL_PLAZA, clean, f"commercial plaza name: {hit}")

    # Tier 3: viable but only a landmark proxy (supermarket, store, restaurant,
    # news office, random business).
    return _make(AREA_PROXY, clean or "Nearby landmark",
                 "viable nearby landmark, not a commercial site")


def _first_hint(name_lower: str, hints: Tuple[str, ...]) -> Optional[str]:
    # Longest hints first so "medical office building" beats "office building".
    for hint in sorted(hints, key=len, reverse=True):
        if hint in name_lower:
            return hint
    return None


def _make(status: str, display_name: str, reason: str) -> AnchorAssessment:
    return AnchorAssessment(
        anchor_status=status,
        anchor_status_label=STATUS_LABELS[status],
        anchor_quality_score=STATUS_QUALITY[status],
        anchor_display_name=display_name,
        site_score_withheld=status in _SITE_SCORE_WITHHELD,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Area / display naming
# --------------------------------------------------------------------------- #

_STREET_NUM_RE = re.compile(r"^\s*\d+[A-Za-z]?\s+")
_DIRECTIONAL = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


def _street_corridor(formatted_address: str) -> str:
    """Best-effort corridor name from a street address, e.g.
    '420 S 2nd St, San Jose, CA 95113' -> 'S 2nd St'."""
    first = str(formatted_address or "").split(",")[0].strip()
    if not first:
        return ""
    street = _STREET_NUM_RE.sub("", first).strip()
    # A bare directional or empty result isn't a useful corridor.
    if not street or street.lower() in _DIRECTIONAL:
        return ""
    return street


def area_display_name(profile: Dict[str, Any]) -> str:
    """A human area/corridor name for the candidate — never the random POI.

    Prefers an explicit neighborhood when the pipeline has one; otherwise builds
    a street-corridor + city label; otherwise just the city/state area.
    """
    city = str(profile.get("city") or "").strip()
    state = str(profile.get("state") or "").strip()
    neighborhood = str(
        profile.get("neighborhood") or profile.get("sublocality") or ""
    ).strip()
    if neighborhood and city:
        return f"{neighborhood}, {city}"
    if neighborhood:
        return neighborhood

    anchor = profile.get("anchor") or {}
    corridor = _street_corridor(anchor.get("formatted_address") or "")
    if corridor and city:
        return f"{city} — {corridor} corridor"
    if city and state:
        return f"{city}, {state} area"
    return city or state or "Candidate area"


def assess_anchor(profile: Dict[str, Any]) -> AnchorAssessment:
    """Anchor assessment for a candidate profile.

    Uses stored ``anchor_status`` fields when present (pipeline already ran),
    otherwise classifies live from the profile's ``anchor`` dict so existing
    scored JSON works without re-running the API pipeline.
    """
    stored = profile.get("anchor_status")
    if isinstance(stored, dict) and stored.get("anchor_status"):
        return AnchorAssessment(
            anchor_status=stored.get("anchor_status"),
            anchor_status_label=stored.get(
                "anchor_status_label",
                STATUS_LABELS.get(stored.get("anchor_status"), ""),
            ),
            anchor_quality_score=int(stored.get("anchor_quality_score") or 0),
            anchor_display_name=stored.get("anchor_display_name", ""),
            site_score_withheld=bool(stored.get("site_score_withheld")),
            reason=stored.get("reason", ""),
        )
    anchor = profile.get("anchor") or {}
    return classify_anchor(
        types=anchor.get("types") or [],
        name=anchor.get("name") or "",
        formatted_address=anchor.get("formatted_address") or "",
    )


# Validation checklist surfaced for area_proxy (and invalid) anchors: the work
# needed before the area can be treated as an actual site.
AREA_PROXY_CHECKLIST: Tuple[str, ...] = (
    "Find an actual commercial unit to lease",
    "Confirm rent",
    "Confirm parking",
    "Confirm classroom access",
    "Confirm ADA / restrooms",
    "Confirm weekend / evening access",
)
