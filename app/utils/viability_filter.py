"""
Commercial-viability filter for candidate anchors.

Some grid points fall on locations that are not realistic ALLCPR sites even
though Google Places returns them as the closest "anchor" — rail yards,
rental-car return lots, military bases, treatment plants, freight depots,
heavy-industrial facilities, prisons. They generate noise in the ranked
report.

This module is intentionally conservative: it only excludes anchors that
clearly match a hard blacklist of types or name patterns. Anything ambiguous
is treated as viable. The result is a (viable, reason) tuple so callers can
record *why* something was rejected.
"""
from __future__ import annotations

import re
from typing import Iterable, Tuple


NON_COMMERCIAL_TYPES: frozenset = frozenset({
    # Transit infrastructure — buses, BART, light rail, subway, freight yards.
    # In dense urban areas Google Places returns these constantly as
    # "closest anchor" but none of them is a leasable storefront.
    "transit_station",
    "transit_depot",
    "bus_station",
    "subway_station",
    "light_rail_station",
    "train_station",
    # Public-safety facilities (closed to public retail traffic).
    "police",
    "fire_station",
    "prison",
    # Cemeteries / funeral homes — not commercially viable storefronts.
    "cemetery",
    "funeral_home",
    # Government / restricted-access facilities.
    "embassy",
    "courthouse",
    "local_government_office",
    "post_office",
    # Roads, intersections, raw geographies — never an actual business.
    "route",
    "intersection",
    "street_address",
    "premise",
    "subpremise",
    "natural_feature",
    "political",
    # Parking lots / structures — not retail space.
    "parking",
    # Airport infrastructure (terminals are not leasable to a CPR center).
    "airport",
    # Heavy industrial / utility (already covered by name patterns but the
    # type is more reliable when present).
    "storage",
})


# Commercial storefront signals. An anchor must carry AT LEAST ONE of these
# types — or match a known commercial name pattern — to be treated as a
# realistic leasable site. Anything else is "needs commercial site validation".
COMMERCIAL_HINT_TYPES: frozenset = frozenset({
    "shopping_mall",
    "store",
    "clothing_store",
    "convenience_store",
    "department_store",
    "electronics_store",
    "grocery_or_supermarket",
    "home_goods_store",
    "supermarket",
    "book_store",
    "furniture_store",
    "hardware_store",
    "drugstore",
    "pharmacy",
    "shoe_store",
    "bicycle_store",
    "florist",
    "pet_store",
    "jewelry_store",
    "liquor_store",
    "movie_theater",
    "gym",
    # Healthcare / training (already a target use case for ALLCPR).
    "hospital",
    "doctor",
    "dentist",
    "physiotherapist",
    "school",
    "university",
    "library",
    "health",
    # Office / coworking-style buildings.
    "real_estate_agency",
    "insurance_agency",
    "lawyer",
    "accounting",
})

# Commercial-leaning name fragments — used when types are too generic.
_COMMERCIAL_NAME_HINTS = (
    "medical office building", "medical office", "medical plaza",
    "medical center", "medical building",
    "office building", "office tower", "office plaza", "office park",
    "professional building", "business park", "business center",
    "corporate center", "training center", "learning center",
    "education center", "executive suites",
    "retail center", "retail plaza", "shopping plaza", "shopping center",
    "co-working", "coworking", "wework",
    "plaza", "mall", "centre", "marketplace", "shopping", "center",
)


# Name patterns that strongly suggest infrastructure-only or non-retail.
_BAD_NAME_PATTERNS = [
    re.compile(r"\brail\s*yard\b", re.I),
    re.compile(r"\bswitching\s*yard\b", re.I),
    re.compile(r"\bintermodal\b", re.I),
    re.compile(r"\bfreight\s+(depot|terminal|yard)\b", re.I),
    re.compile(r"\brental\s*car\b", re.I),
    re.compile(r"\bcar\s+rental\b", re.I),
    re.compile(r"\bsubstation\b", re.I),
    re.compile(r"\bpower\s+plant\b", re.I),
    re.compile(r"\b(water|wastewater|sewage)\s+treatment\b", re.I),
    re.compile(r"\bpumping\s+station\b", re.I),
    re.compile(r"\blandfill\b", re.I),
    re.compile(r"\brefinery\b", re.I),
    re.compile(r"\b(oil|gas)\s+terminal\b", re.I),
    re.compile(r"\bquarry\b", re.I),
    re.compile(r"\bcorrectional\b", re.I),
    re.compile(r"\bmilitary\s+base\b", re.I),
    re.compile(r"\barmory\b", re.I),
    re.compile(r"\bcustoms\b", re.I),
    re.compile(r"\bcargo\b", re.I),
    re.compile(r"\bport\s+(authority|terminal)\b", re.I),
    re.compile(r"\bcell\s+tower\b", re.I),
    re.compile(r"\bdata\s+center\b", re.I),
    re.compile(r"\b(distribution|fulfillment)\s+center\b", re.I),
    # Explicit "not a public stop" markers Google sometimes returns.
    re.compile(r"\bnot\s+a\s+public\s+stop\b", re.I),
    # Parking lot / structure / garage names.
    re.compile(r"\bparking\s+(lot|garage|structure|deck)\b", re.I),
    # Bus stop / bus shelter (when type missing but name present).
    re.compile(r"\bbus\s+(stop|shelter)\b", re.I),
    # BART / subway / light-rail station names (e.g. "Montgomery", "Powell").
    re.compile(r"\b(bart|caltrain|amtrak|muni)\s+station\b", re.I),
    re.compile(r"\b(subway|metro)\s+station\b", re.I),
    # Google Plus Code reverse-geocode fallback (e.g. "RJ38+HW Embarcadero, …").
    # These are coordinate codes, not business names.
    re.compile(r"^[2-9CFGHJMPQRVWX]{4,8}\+[2-9CFGHJMPQRVWX]{2,3}\b", re.I),
    # Ambulance bays / loading docks / receiving areas — non-public sides of
    # otherwise-public buildings. Tagged as hospital/establishment but never
    # leasable space.
    re.compile(r"\bambulance\s+bay\b", re.I),
    re.compile(r"\bloading\s+dock\b", re.I),
    re.compile(r"\breceiving\s+(bay|dock|area)\b", re.I),
    re.compile(r"\bemergency\s+(entrance|exit|bay)\b", re.I),
    re.compile(r"\bservice\s+entrance\b", re.I),
]

# Spam / scam Google Maps listings — fake "businesses" created to spam search
# results (gift-card generators, game-currency hacks, crypto giveaways, "make
# money fast"). These are never real leasable storefronts and must never rank.
# Conservative: each pattern requires an unambiguous scam phrase, so legitimate
# names ("Free People", "Generator Coffee", "Bitcoin Depot") are NOT caught.
_SPAM_NAME_PATTERNS = [
    re.compile(r"\bgift\s*cards?\s+generator\b", re.I),
    re.compile(r"\bfree\b[\w\s'.\-]{0,30}\bgift\s*cards?\b", re.I),
    re.compile(r"\b(free|unlimited)\b[\w\s'.\-]{0,20}"
               r"\b(generator|robux|v-?bucks|bitcoins?|coins?|gems?|"
               r"crypto|giveaways?|followers?|likes?|wallet\s+codes?|"
               r"redeem\s+codes?|promo\s+codes?)\b", re.I),
    re.compile(r"\b(robux|v-?bucks|psn|xbox|steam|itunes|netflix|spotify)\s+"
               r"(generator|codes?|free|hack)\b", re.I),
    # "<thing> giveaway free", "bitcoin giveaway" — keyword-then-modifier order.
    re.compile(r"\bgiveaways?\b[\w\s'.\-]{0,20}"
               r"\b(free|bitcoins?|crypto|cash|cards?)\b", re.I),
    re.compile(r"\bno\s+(human\s+)?(survey|verification)\b", re.I),
    re.compile(r"\b(aimbot|mod\s*apk|cheat\s+codes?)\b", re.I),
    re.compile(r"\bmake\s+money\s+(fast|online|now|today|from\s+home)\b", re.I),
    re.compile(r"\b100%\s*free\b", re.I),
    re.compile(r"\bclick\s+here\b", re.I),
]


# Intersection patterns: "Hayes St & Divisadero St", "Polk St & Pine St",
# "California St & Pierce St". Cross streets with the "&" or "and" joiner
# and at least one street-suffix on either side.
_STREET_SUFFIX = (
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Way|Dr|Drive|"
    r"Pl|Place|Ln|Lane|Ct|Court|Pkwy|Parkway|Hwy|Highway|Ter|Terrace)"
)
_INTERSECTION_RE = re.compile(
    rf"\b\w[\w'.\- ]*?\s+{_STREET_SUFFIX}\.?\s*(?:&|and)\s*"
    rf"\w[\w'.\- ]*?\s+{_STREET_SUFFIX}\b",
    re.I,
)


def is_anchor_viable(
    types: Iterable[str] = (),
    name: str = "",
    formatted_address: str = "",
) -> Tuple[bool, str]:
    """Return ``(viable, reason)`` for a candidate anchor.

    ``reason`` is a short human-readable string when ``viable`` is False, or
    ``""`` when the anchor passes. Used by ``anchor.select_anchor`` to prune
    the pool and by the pipeline to drop industrial-only / transit-only /
    intersection candidates from the ranked report.
    """
    type_set = {str(t).lower() for t in (types or [])}
    bad_types = type_set & NON_COMMERCIAL_TYPES
    if bad_types:
        return False, f"non-commercial place type: {sorted(bad_types)[0]}"

    name_str = (name or "").strip()
    for pat in _SPAM_NAME_PATTERNS:
        if pat.search(name_str):
            return False, "spam/scam listing (fake Google Maps business)"

    for pat in _BAD_NAME_PATTERNS:
        if pat.search(name_str):
            return False, f"non-commercial name pattern: {pat.pattern}"

    if _INTERSECTION_RE.search(name_str):
        return False, "intersection-style name (cross-streets)"

    # Address-based check is intentionally weak — many legitimate retail
    # storefronts sit on industrial-adjacent streets — so we only check
    # for very explicit hits in the address line.
    addr = (formatted_address or "").lower()
    if "rail yard" in addr or "freight terminal" in addr:
        return False, "non-commercial address indicator"

    return True, ""


def has_commercial_signal(
    types: Iterable[str] = (),
    name: str = "",
) -> Tuple[bool, str]:
    """Return ``(is_commercial, reason)`` for a viable anchor.

    "Viable" (per :func:`is_anchor_viable`) only filters out the obviously
    bad — transit stops, intersections, government offices. A second pass
    is needed for the SF-style failure mode where the anchor *is* an
    establishment but not a leasable storefront: corporate HQs, random LLCs,
    legal-name pass-through reverse-geocode hits.

    Returns ``True`` when the anchor carries a commercial type hint or a
    known commercial name fragment ("plaza", "office tower", "training
    center", etc). Otherwise the caller should label the candidate as
    "Needs commercial site validation" rather than treat the anchor as a
    confirmed business location.
    """
    type_set = {str(t).lower() for t in (types or [])}
    hits = type_set & COMMERCIAL_HINT_TYPES
    if hits:
        return True, f"commercial type: {sorted(hits)[0]}"

    name_lower = (name or "").lower()
    # Most specific multi-word hints win — sort by length descending so
    # "medical office" matches before "office building" matches before
    # "office", and "training center" before "center".
    for hint in sorted(_COMMERCIAL_NAME_HINTS, key=len, reverse=True):
        if hint in name_lower:
            return True, f"commercial name hint: {hint}"

    return False, "no commercial type or name hint"
