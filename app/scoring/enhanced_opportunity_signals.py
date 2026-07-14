"""
Enhanced (Phase-2) modeled-opportunity signals from Google Places / POI evidence.

This module turns raw POI evidence (Places results, or pre-counted POIs) plus the
ZIP's Census Gazetteer land area into the three *enhanced* signals consumed by
:func:`app.scoring.zip_modeled_opportunity.compute_zip_modeled_opportunity`:

  * ``healthcare_facility_density``  — healthcare POIs per square mile.
  * ``training_school_density``      — CPR/BLS-relevant training POIs per sq mile.
  * ``competition_gap_score``        — 0..100, high = little direct competition.

Design rules (mirror the rest of the modeled layer):
  * Nothing is hardcoded or guessed. A density needs both a real POI count and a
    real land area; if either is missing the signal is ``None`` and the scorer
    drops it (renormalizing the remaining weights) — never invents a value.
  * Counts come from the cached Google Places queries the enrichment run already
    makes (see :mod:`scripts.enrich_top_zips`); this module only classifies and
    normalizes them, so it is pure and unit-testable with no network.
  * The competition formula is explicit and configurable
    (:data:`app.config.COMPETITION_SATURATION_COUNT`).

This is a leaf module: it imports only :mod:`app.config`. The scoring module does
NOT import it, so there is no import cycle.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.config import COMPETITION_SATURATION_COUNT

# --------------------------------------------------------------------------- #
# POI category catalogs.
#
# A POI matches a category when its Google Places ``types`` intersect the type
# set OR its name contains one of the keywords (case-insensitive). Keyword
# matching keeps coverage robust across mixed result sets and the targeted
# keyword queries the enrichment run uses.
# --------------------------------------------------------------------------- #

# Healthcare facilities: hospitals, urgent care, clinics, medical centers,
# doctor offices, nursing facilities, and similar.
HEALTHCARE_FACILITY_TYPES = frozenset({
    "hospital",
    "doctor",
    "physiotherapist",
    "medical_lab",
    "wellness_center",
    "skilled_nursing_facility",
})
HEALTHCARE_FACILITY_KEYWORDS = (
    "hospital",
    "urgent care",
    "clinic",
    "medical center",
    "medical centre",
    "medical group",
    "doctor",
    "physician",
    "health center",
    "health centre",
    "family medicine",
    "primary care",
    "pediatric",
    "cardiology",
    "dialysis",
    "surgery center",
    "nursing home",
    "nursing facility",
    "skilled nursing",
    "rehabilitation center",
)

# Training/education POIs relevant to CPR/BLS demand: CPR/BLS training centers,
# nursing schools, EMT schools, medical-assistant schools, vocational healthcare
# programs, and community-college healthcare programs.
TRAINING_SCHOOL_KEYWORDS = (
    "cpr",
    "bls",
    "acls",
    "pals",
    "first aid",
    "life support",
    "nursing school",
    "school of nursing",
    "nursing program",
    "emt",
    "emergency medical technician",
    "paramedic",
    "medical assistant",
    "phlebotomy",
    "cna ",
    "certified nursing assistant",
    "allied health",
    "health science",
    "vocational",
    "community college",
)

# Direct competitors: CPR / BLS / first-aid certification providers.
COMPETITOR_KEYWORDS = (
    "cpr",
    "bls",
    "acls",
    "pals",
    "first aid",
    "basic life support",
    "advanced cardiac life support",
    "aha training",
    "red cross training",
    "heartsaver",
)


# --------------------------------------------------------------------------- #
# POI classification helpers
# --------------------------------------------------------------------------- #
def _place_name(place: Any) -> str:
    if isinstance(place, dict):
        name = place.get("name") or place.get("displayName") or ""
        if isinstance(name, dict):  # Places API (New) {"text": ...}
            name = name.get("text", "")
        return str(name or "")
    return str(place or "")


def _place_types(place: Any) -> List[str]:
    if isinstance(place, dict):
        return [str(t).lower() for t in (place.get("types") or [])]
    return []


def _matches(place: Any, types: Sequence[str], keywords: Sequence[str]) -> bool:
    if types and set(_place_types(place)) & set(types):
        return True
    name = _place_name(place).lower()
    return any(kw in name for kw in keywords)


def _dedupe(places: Sequence[Any]) -> List[Any]:
    """Drop duplicate POIs (same place_id) so overlapping queries never double-count."""
    seen: set = set()
    out: List[Any] = []
    for place in places or []:
        pid = place.get("place_id") if isinstance(place, dict) else None
        key = pid or id(place)
        if key in seen:
            continue
        seen.add(key)
        out.append(place)
    return out


def count_healthcare_facilities(places: Sequence[Any]) -> int:
    return sum(
        1 for p in _dedupe(places)
        if _matches(p, HEALTHCARE_FACILITY_TYPES, HEALTHCARE_FACILITY_KEYWORDS)
    )


def count_training_schools(places: Sequence[Any]) -> int:
    return sum(
        1 for p in _dedupe(places)
        if _matches(p, (), TRAINING_SCHOOL_KEYWORDS)
    )


def count_competitors(places: Sequence[Any]) -> int:
    return sum(
        1 for p in _dedupe(places)
        if _matches(p, (), COMPETITOR_KEYWORDS)
    )


# --------------------------------------------------------------------------- #
# Normalization + scoring primitives
# --------------------------------------------------------------------------- #
def density_per_sq_mile(count: Optional[float],
                        land_sqmi: Optional[float]) -> Optional[float]:
    """POIs per square mile, or ``None`` when either input is missing.

    A density is real only with both a real POI count and a real land area. A
    missing/zero area yields ``None`` so the signal drops out and the modeled
    weights renormalize — it never silently becomes a misleading 0 or infinity.
    """
    if count is None or land_sqmi is None:
        return None
    try:
        area = float(land_sqmi)
        n = float(count)
    except (TypeError, ValueError):
        return None
    if area <= 0:
        return None
    return round(n / area, 4)


def competition_gap_fraction(
    competitor_count: Optional[float],
    saturation_count: float = COMPETITION_SATURATION_COUNT,
) -> Optional[float]:
    """Transparent 0..1 gap: ``1 - min(1, competitor_count / saturation_count)``.

    Fewer competitors → larger gap → higher value. ``competitor_count`` near 0
    approaches 1.0; at/above ``saturation_count`` (default 20) it is 0.0.
    """
    if competitor_count is None:
        return None
    try:
        c = max(0.0, float(competitor_count))
    except (TypeError, ValueError):
        return None
    s = max(1e-9, float(saturation_count))
    return max(0.0, min(1.0, 1.0 - c / s))


def competition_gap_score(
    competitor_count: Optional[float],
    saturation_count: float = COMPETITION_SATURATION_COUNT,
) -> Optional[float]:
    """:func:`competition_gap_fraction` scaled to the model's 0..100 feature range.

    0..100 matches ``ZIP_MODEL_BOUNDS["competition_gap_score"]`` (high = little
    competition), so the value can be fed straight into the modeled scorer.
    """
    frac = competition_gap_fraction(competitor_count, saturation_count)
    return None if frac is None else round(100.0 * frac, 1)


# --------------------------------------------------------------------------- #
# End-to-end enhanced-signal computation
# --------------------------------------------------------------------------- #
def compute_enhanced_signals(
    *,
    land_sqmi: Optional[float],
    healthcare_count: Optional[float] = None,
    training_count: Optional[float] = None,
    competitor_count: Optional[float] = None,
    healthcare_places: Optional[Sequence[Any]] = None,
    training_places: Optional[Sequence[Any]] = None,
    competitor_places: Optional[Sequence[Any]] = None,
    saturation_count: float = COMPETITION_SATURATION_COUNT,
) -> Dict[str, Any]:
    """Derive the three enhanced signals + a debug breakdown.

    Supply either pre-counted POIs (``*_count``, e.g. from cached targeted Places
    queries) or raw Places result lists (``*_places``) to classify here. Returns
    a dict with the three feature values (``None`` when not derivable) under their
    canonical keys, plus ``debug`` showing raw counts, ZIP area, and density.
    """
    if healthcare_count is None and healthcare_places is not None:
        healthcare_count = count_healthcare_facilities(healthcare_places)
    if training_count is None and training_places is not None:
        training_count = count_training_schools(training_places)
    if competitor_count is None and competitor_places is not None:
        competitor_count = count_competitors(competitor_places)

    healthcare_density = density_per_sq_mile(healthcare_count, land_sqmi)
    training_density = density_per_sq_mile(training_count, land_sqmi)
    gap_fraction = competition_gap_fraction(competitor_count, saturation_count)
    gap_score = None if gap_fraction is None else round(100.0 * gap_fraction, 1)

    debug = {
        "land_sqmi": (round(float(land_sqmi), 4)
                      if land_sqmi not in (None, "") else None),
        "saturation_count": saturation_count,
        "healthcare_facility_density": {
            "raw_count": healthcare_count,
            "density_per_sqmi": healthcare_density,
        },
        "training_school_density": {
            "raw_count": training_count,
            "density_per_sqmi": training_density,
        },
        "competition_gap_score": {
            "competitor_count": competitor_count,
            "gap_fraction": (round(gap_fraction, 4)
                             if gap_fraction is not None else None),
            "gap_score": gap_score,
        },
    }
    return {
        "healthcare_facility_density": healthcare_density,
        "training_school_density": training_density,
        "competition_gap_score": gap_score,
        "debug": debug,
    }
