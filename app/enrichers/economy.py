"""
Economy enricher.

Combines Census ACS + BLS-stub + real-estate-stub into a single record
attached to a candidate. We deliberately keep the structure flat and
None-friendly so downstream code never has to guess.
"""
from __future__ import annotations

from typing import Dict

from app.collectors import bls_or_labor, census, real_estate
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


def collect_economy_for_point(latitude: float, longitude: float,
                              city: str = "", state: str = "") -> Dict[str, object]:
    """
    Returns:
        {
          "census":      {values, indicators, sources},
          "labor":       {values, indicators, sources},
          "real_estate": {values, indicators, sources},
        }
    """
    return {
        "census": census.collect_economy(latitude, longitude),
        "labor": bls_or_labor.collect_labor(latitude, longitude),
        "real_estate": real_estate.collect_real_estate(city, state, latitude, longitude),
    }
