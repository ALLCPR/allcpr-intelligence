"""Candidate deduplication helpers."""
from __future__ import annotations

from typing import Dict, List, Tuple

from app.utils.geo_utils import haversine_miles


RankedCandidate = Tuple[Dict, Dict]


def _rank_value(scored: Dict) -> float:
    """Ranking number: area_score (always present) with a site_score fallback
    for legacy inputs."""
    for key in ("area_score", "site_score"):
        v = scored.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def deduplicate_ranked_candidates(
    ranked: List[RankedCandidate],
    min_distance_miles: float,
) -> List[RankedCandidate]:
    """Keep the highest-scoring candidate within each distance cluster."""
    if min_distance_miles <= 0:
        return list(ranked)

    ordered = sorted(ranked, key=lambda ps: _rank_value(ps[1]), reverse=True)
    kept: List[RankedCandidate] = []
    for profile, scored in ordered:
        lat = profile.get("latitude")
        lon = profile.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            kept.append((profile, scored))
            continue
        is_duplicate = False
        for kept_profile, _ in kept:
            kept_lat = kept_profile.get("latitude")
            kept_lon = kept_profile.get("longitude")
            if not isinstance(kept_lat, (int, float)) or not isinstance(kept_lon, (int, float)):
                continue
            if haversine_miles((lat, lon), (kept_lat, kept_lon)) < min_distance_miles:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append((profile, scored))
    kept.sort(key=lambda ps: _rank_value(ps[1]), reverse=True)
    return kept
