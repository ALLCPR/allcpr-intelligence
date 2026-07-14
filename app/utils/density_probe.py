"""
Dense-metro auto-detection.

A 7-mile radius is reasonable for Modesto and absurd for San Francisco. The
pipeline currently lets users configure a radius that blends neighborhoods
in dense metros (the v4 SF report warned about it but didn't auto-correct).
This module probes the configured radius for CPR/BLS competitor density and
auto-reduces radius + grid spacing when the area is dense enough that the
original config will compress every candidate into the same numbers.

A single ``text_search("CPR training", center, radius)`` probe is enough —
20+ competitors within the configured radius is the threshold. Suburban
markets typically return 1-5 results at the same radius, well below.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from app.collectors.google_places import GooglePlacesClient, miles_to_meters
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


# Densities above this trigger dense-mode rescaling.
DENSE_COMPETITOR_THRESHOLD = 20

# In dense mode, cap radius and grid at these neighborhood-scale values.
# Picked so a single grid point's catchment is roughly one SF neighborhood
# (Mission ≠ Pacific Heights ≠ SoMa).
DENSE_RADIUS_MILES = 2.0
DENSE_GRID_SPACING_MILES = 0.6


@dataclass
class DensityProbe:
    """Result of one probe at the configured radius."""
    competitor_count: int
    radius_miles: float
    is_dense: bool
    # Recommended new values when dense — caller decides whether to apply.
    recommended_radius_miles: float
    recommended_grid_spacing_miles: float
    reason: str


def probe_density(
    client: GooglePlacesClient,
    center: Tuple[float, float],
    radius_miles: float,
    grid_spacing_miles: float,
    threshold: int = DENSE_COMPETITOR_THRESHOLD,
    dense_radius: float = DENSE_RADIUS_MILES,
    dense_grid: float = DENSE_GRID_SPACING_MILES,
) -> DensityProbe:
    """One Places text_search probe; return density + recommended rescale.

    A single API call. Cheap. Cached by GooglePlacesClient like every other
    text_search, so repeat runs at the same coordinate don't re-bill.
    """
    radius_m = miles_to_meters(radius_miles)
    try:
        results = client.text_search(
            "CPR training",
            location=center,
            radius_meters=radius_m,
            max_pages=1,
        )
    except Exception as exc:
        logger.warning(f"density_probe: text_search failed: {exc}")
        return DensityProbe(
            competitor_count=0,
            radius_miles=radius_miles,
            is_dense=False,
            recommended_radius_miles=radius_miles,
            recommended_grid_spacing_miles=grid_spacing_miles,
            reason=f"probe failed ({exc.__class__.__name__}); staying in default mode",
        )

    count = len(results or [])
    is_dense = count >= threshold

    if is_dense:
        new_radius = min(radius_miles, dense_radius)
        new_grid = min(grid_spacing_miles, dense_grid)
        reason = (
            f"probe found {count} CPR providers within {radius_miles:.1f}mi "
            f"(threshold {threshold}); switching to dense-metro mode: "
            f"radius {radius_miles:.1f}→{new_radius:.1f}mi, "
            f"grid {grid_spacing_miles:.1f}→{new_grid:.1f}mi"
        )
    else:
        new_radius = radius_miles
        new_grid = grid_spacing_miles
        reason = (
            f"probe found {count} CPR providers within {radius_miles:.1f}mi "
            f"(threshold {threshold}); keeping configured radius and grid"
        )

    return DensityProbe(
        competitor_count=count,
        radius_miles=radius_miles,
        is_dense=is_dense,
        recommended_radius_miles=new_radius,
        recommended_grid_spacing_miles=new_grid,
        reason=reason,
    )
