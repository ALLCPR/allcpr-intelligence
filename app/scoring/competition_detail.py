"""
Competition detail — distance bands, direct-vs-general split, quality, and a
plain-language pressure band.

The raw ``competition_summary`` already aggregates competitor counts, ratings,
reviews and weakness flags. This module reshapes them into the decision-useful
views the report needs:

- distance bands 0–1 / 1–3 / 3–5 mi (derived from the existing per-bucket counts)
- direct CPR/BLS competitors vs general medical/education places (keyword split
  over the competitor list)
- quality roll-up (avg rating, total reviews, website presence, booking /
  schedule friction)
- ``competition_pressure_band``: Low | Medium | High | Extreme (config cutoffs
  on the existing ``competition_pressure_score``)

Nothing is fabricated — missing inputs yield ``None`` / "Unknown".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import PRESSURE_BAND_EXTREME, PRESSURE_BAND_LOW, PRESSURE_BAND_MEDIUM

# Tokens that mark a competitor as a *direct* CPR/BLS/first-aid trainer.
_DIRECT_TOKENS = (
    "cpr", "bls", "acls", "pals", "first aid", "first-aid", "life support",
    "basic life", "aha", "american heart", "red cross", "heartsaver",
    "cna training", "ems training", "emt training", "certification",
)


@dataclass
class CompetitionDetail:
    band_0_1_mi: int
    band_1_3_mi: int
    band_3_5_mi: int
    direct_competitors: int
    general_competitors: int
    avg_rating: Optional[float]
    total_reviews: int
    website_presence: Optional[float]      # 0..1 share with a website
    booking_friction_share: Optional[float]
    schedule_unavailable_share: Optional[float]
    competition_pressure_band: str          # Low | Medium | High | Extreme | Unknown
    rationale: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "band_0_1_mi": self.band_0_1_mi,
            "band_1_3_mi": self.band_1_3_mi,
            "band_3_5_mi": self.band_3_5_mi,
            "direct_competitors": self.direct_competitors,
            "general_competitors": self.general_competitors,
            "avg_rating": self.avg_rating,
            "total_reviews": self.total_reviews,
            "website_presence": self.website_presence,
            "booking_friction_share": self.booking_friction_share,
            "schedule_unavailable_share": self.schedule_unavailable_share,
            "competition_pressure_band": self.competition_pressure_band,
            "rationale": self.rationale,
        }


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def pressure_band(score: Optional[float]) -> str:
    if score is None:
        return "Unknown"
    if score < PRESSURE_BAND_LOW:
        return "Low"
    if score < PRESSURE_BAND_MEDIUM:
        return "Medium"
    if score < PRESSURE_BAND_EXTREME:
        return "High"
    return "Extreme"


def _is_direct(competitor: Dict[str, Any]) -> bool:
    hay = " ".join([
        str(competitor.get("name") or ""),
        str(competitor.get("category") or ""),
        " ".join(competitor.get("types") or []),
    ]).lower()
    return any(tok in hay for tok in _DIRECT_TOKENS)


def compute_competition_detail(
    profile: Dict[str, Any],
    competition_summary: Dict[str, Any],
    competition_pressure_score: Optional[float],
) -> CompetitionDetail:
    bucket: Dict[Any, int] = competition_summary.get("competitor_count_by_bucket_mi") or {}
    # Bucket keys may be int or str depending on JSON round-trip.
    def b(mi: int) -> int:
        return _i(bucket.get(mi, bucket.get(str(mi), 0)))

    b1, b3, b5 = b(1), b(3), b(5)
    band_0_1 = b1
    band_1_3 = max(0, b3 - b1)
    band_3_5 = max(0, b5 - b3)

    competitors: List[Dict[str, Any]] = profile.get("competitors") or []  # type: ignore[assignment]
    direct = sum(1 for c in competitors if isinstance(c, dict) and _is_direct(c))
    general = max(0, len(competitors) - direct)

    total = _i(competition_summary.get("competitor_count_total"))
    avg_rating = competition_summary.get("competitor_avg_rating")
    avg_rating = float(avg_rating) if isinstance(avg_rating, (int, float)) else None
    total_reviews = _i(competition_summary.get("competitor_total_reviews"))

    no_website = _i(competition_summary.get("competitor_no_website"))
    booking_missing = _i(competition_summary.get("competitor_online_booking_missing"))
    schedule_missing = _i(competition_summary.get("competitor_class_schedule_missing"))
    website_presence = round(1.0 - no_website / total, 3) if total > 0 else None
    booking_friction = round(booking_missing / total, 3) if total > 0 else None
    schedule_unavailable = round(schedule_missing / total, 3) if total > 0 else None

    band = pressure_band(competition_pressure_score)

    rationale: List[str] = [
        f"{band_0_1} within 1 mi · {band_1_3} in 1–3 mi · {band_3_5} in 3–5 mi",
        f"{direct} direct CPR/BLS vs {general} general medical/education",
        f"competition pressure: {band}"
        + (f" ({competition_pressure_score:.0f}/100)"
           if competition_pressure_score is not None else ""),
    ]
    if website_presence is not None and website_presence < 0.6:
        rationale.append(
            f"only {website_presence:.0%} of competitors have a website — "
            f"digital-first entry advantage"
        )

    return CompetitionDetail(
        band_0_1_mi=band_0_1,
        band_1_3_mi=band_1_3,
        band_3_5_mi=band_3_5,
        direct_competitors=direct,
        general_competitors=general,
        avg_rating=round(avg_rating, 2) if avg_rating is not None else None,
        total_reviews=total_reviews,
        website_presence=website_presence,
        booking_friction_share=booking_friction,
        schedule_unavailable_share=schedule_unavailable,
        competition_pressure_band=band,
        rationale=rationale,
    )
