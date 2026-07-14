"""
NPPES / NPI healthcare-provider density by ZIP (streaming).

The full NPPES download is a single ~9 GB CSV of every US healthcare provider.
We never load it into memory: ``aggregate_npi_by_zip`` streams it row by row,
classifies each provider by taxonomy, and keeps only small per-ZIP integer
counters. A ``--sample`` path (tiny CSV) makes the whole thing runnable offline
and unit-testable.

Output per ZIP (provider DENSITY per 10k population is computed later, in the
build script, where ACS population is available):
    healthcare_provider_count, nurse_count, physician_count, clinic_provider_count

Taxonomy classification is a documented heuristic on the NUCC taxonomy code
prefix — good enough for a density signal, not a clinical taxonomy. Real data:
download "NPPES Data Dissemination" (npidata_pfile_*.csv), drop under
data/raw/bulk/, and point the build script at it.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# NPPES headers are long; match by case-insensitive substring so the schema can
# drift slightly without breaking us.
_ZIP_HEADER = "postal code"            # "Provider Business Practice Location ... Postal Code"
_ZIP_HEADER_PRACTICE = "practice location address postal code"
_TAXONOMY_HEADER = "taxonomy code_1"   # "Healthcare Provider Taxonomy Code_1"
_ENTITY_HEADER = "entity type code"    # 1 = individual, 2 = organization

# Taxonomy code prefixes (NUCC). Heuristic — see module docstring.
_NURSE_PREFIXES = ("163", "164", "367", "364", "366")   # RN/LPN/NP/CNS/midwife
_PHYSICIAN_PREFIXES = ("20",)                            # MD/DO specialties


def classify_taxonomy(code: Optional[str]) -> str:
    """Return 'nurse' | 'physician' | 'other' from a NUCC taxonomy code."""
    if not code:
        return "other"
    c = str(code).strip().upper()
    if c.startswith(_NURSE_PREFIXES):
        return "nurse"
    if c.startswith(_PHYSICIAN_PREFIXES):
        return "physician"
    return "other"


def _find_header(fieldnames, needle: str) -> Optional[str]:
    for name in fieldnames or ():
        if needle in name.lower():
            return name
    return None


def stream_npi_rows(path: Path) -> Iterator[Dict[str, str]]:
    """Yield NPPES rows one at a time (constant memory). ``[]`` if missing."""
    p = Path(path)
    if not p.exists():
        logger.warning(f"NPI: file not found, skipping: {p}")
        return
    with p.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def aggregate_npi_by_zip(
    path: Path,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Dict[str, int]]:
    """Stream the NPPES file and aggregate provider counts per ZIP.

    ``limit`` caps rows processed (dev/sample). Constant memory aside from the
    per-ZIP counters. Missing file → ``{}``.
    """
    p = Path(path)
    if not p.exists():
        logger.warning(f"NPI: file not found, skipping: {p}")
        return {}

    out: Dict[str, Dict[str, int]] = {}
    zip_field = tax_field = entity_field = None
    processed = 0
    try:
        with p.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            zip_field = (_find_header(fields, _ZIP_HEADER_PRACTICE)
                         or _find_header(fields, _ZIP_HEADER))
            tax_field = _find_header(fields, _TAXONOMY_HEADER)
            entity_field = _find_header(fields, _ENTITY_HEADER)
            if not zip_field:
                logger.warning("NPI: no postal-code column found; skipping.")
                return {}
            for row in reader:
                if limit is not None and processed >= limit:
                    break
                processed += 1
                raw_zip = str(row.get(zip_field) or "").strip()[:5]
                if len(raw_zip) != 5 or not raw_zip.isdigit():
                    continue
                bucket = out.setdefault(raw_zip, {
                    "healthcare_provider_count": 0, "nurse_count": 0,
                    "physician_count": 0, "clinic_provider_count": 0})
                bucket["healthcare_provider_count"] += 1
                kind = classify_taxonomy(row.get(tax_field) if tax_field else None)
                if kind == "nurse":
                    bucket["nurse_count"] += 1
                elif kind == "physician":
                    bucket["physician_count"] += 1
                if entity_field and str(row.get(entity_field) or "").strip() == "2":
                    bucket["clinic_provider_count"] += 1
    except (OSError, csv.Error) as exc:
        logger.warning(f"NPI: failed to read {p}: {exc}")
        return {}
    logger.info(f"NPI: aggregated {processed} provider rows into "
                f"{len(out)} ZIPs.")
    return out
