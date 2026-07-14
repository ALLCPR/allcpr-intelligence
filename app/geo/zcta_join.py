"""
Assign lat/lng points to ZIP/ZCTA and aggregate counts per ZIP.

This is the spatial foundation for bulk enrichment: given a national point file
(hospitals, schools, providers…), bin each point into a ZIP so we can produce
per-ZIP counts.

**Method + limitation.** True ZCTA *polygon* containment (Census TIGER
shapefiles) is the gold standard but pulls in heavy geometry deps
(shapely/geopandas) and ~500 MB of shapefiles. We deliberately use the lighter
**nearest-centroid** approximation: each point is assigned to the closest ZIP
centroid within ``max_miles``. This is fast (a lat/lng grid index, no scipy),
dependency-free, and good enough for counting facilities per area. It can
mis-assign a point near a ZIP boundary to a neighboring ZIP — acceptable for
demand-density signals, and documented. A polygon upgrade can drop in behind the
same ``assign_point_to_zcta`` / ``aggregate_points_by_zcta`` interface later.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from app.scoring.zip_demand import load_zip_centroids
from app.utils.geo_utils import haversine_miles

Centroids = Dict[str, Tuple[float, float]]
GridIndex = Dict[Tuple[int, int], List[str]]

# Grid cell size in degrees (~14 mi of latitude). A query checks its own cell
# plus the 8 neighbors, bounding the candidate set without a real spatial tree.
_CELL_DEG = 0.2


def load_zcta_geometries(path: Optional[str] = None) -> Centroids:
    """Load ``{zip: (lat, lng)}`` ZCTA centroids (TIGER polygons are a future
    upgrade — see module docstring). Empty dict when the file is missing."""
    return load_zip_centroids(path)


def _cell(lat: float, lng: float) -> Tuple[int, int]:
    return (int(lat // _CELL_DEG), int(lng // _CELL_DEG))


def build_grid_index(centroids: Centroids) -> GridIndex:
    """Bucket ZIP centroids into a coarse lat/lng grid for fast nearest lookup."""
    index: GridIndex = {}
    for zip_code, (lat, lng) in centroids.items():
        index.setdefault(_cell(lat, lng), []).append(zip_code)
    return index


def assign_point_to_zcta(
    lat: float,
    lng: float,
    centroids: Centroids,
    *,
    index: Optional[GridIndex] = None,
    max_miles: float = 25.0,
) -> Optional[str]:
    """Return the ZIP whose centroid is nearest to ``(lat, lng)``.

    ``None`` when nothing is within ``max_miles`` (avoids assigning a remote
    point to an absurdly far ZIP). Pass a prebuilt ``index`` for speed on large
    point files.
    """
    if lat is None or lng is None or not centroids:
        return None
    if index is None:
        index = build_grid_index(centroids)
    base = _cell(lat, lng)
    candidates: List[str] = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            candidates.extend(index.get((base[0] + dr, base[1] + dc), ()))
    if not candidates:
        # Sparse area — fall back to scanning all centroids.
        candidates = list(centroids)
    best_zip: Optional[str] = None
    best_d = max_miles
    for z in candidates:
        d = haversine_miles((lat, lng), centroids[z])
        if d <= best_d:
            best_d = d
            best_zip = z
    return best_zip


def aggregate_points_by_zcta(
    points: Iterable[Tuple[float, float]],
    centroids: Centroids,
    *,
    max_miles: float = 25.0,
) -> Dict[str, int]:
    """Count how many points fall in each ZIP (nearest-centroid). Points that
    can't be placed (no coords / too far) are dropped, never invented."""
    index = build_grid_index(centroids)
    counts: Dict[str, int] = {}
    for lat, lng in points:
        z = assign_point_to_zcta(lat, lng, centroids, index=index,
                                 max_miles=max_miles)
        if z:
            counts[z] = counts.get(z, 0) + 1
    return counts
