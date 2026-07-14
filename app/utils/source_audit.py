"""Source audit helpers for reports.

The pipeline keeps raw provenance records small. This module turns those
records into report-ready audit rows with a consistent quality/confidence
label, without inventing any missing source metadata.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def source_quality(name: str, url: str = "") -> str:
    """Return a conservative source-quality label for a provenance record."""
    label = (name or "").lower()
    if "stub" in label or "not yet integrated" in label:
        return "stub"
    if "manual rent override" in label or "manual rent overrides" in label:
        return "manual_override"
    if "job postings csv" in label or "job posting" in label:
        return "manual_public_csv"
    if "census" in label or "bls" in label:
        return "official"
    if "google places" in label or "google geocoding" in label:
        return "platform_api"
    if "competitor website" in label or url.startswith(("http://", "https://")):
        return "web_fetch"
    if url:
        return "external"
    return "unknown"


def source_confidence(name: str, fields: Iterable[str], url: str = "") -> str:
    """Return a confidence label based on source type and populated fields."""
    fields = [f for f in fields if f]
    quality = source_quality(name, url)
    if quality == "stub":
        return "none"
    if not fields:
        return "unknown"
    if quality in ("official", "manual_override", "manual_public_csv"):
        return "high"
    if quality == "platform_api":
        return "medium_high"
    if quality == "web_fetch":
        return "medium"
    if quality == "external":
        return "medium_low"
    return "unknown"


def build_source_audit_rows(sources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize source records into report-ready audit rows."""
    rows: List[Dict[str, Any]] = []
    for source in sources or []:
        name = str(source.get("name") or "unknown")
        url = str(source.get("url") or "")
        fields = list(source.get("fields") or [])
        rows.append({
            "source_name": name,
            "source_api_or_url": url or "unknown",
            "retrieved_at": source.get("collected_at") or source.get("retrieved_at") or "unknown",
            "source_quality": source_quality(name, url),
            "fields_populated": fields,
            "confidence": source_confidence(name, fields, url),
            "notes": source.get("notes") or "",
        })
    return rows


def flatten_source_audit_for_candidates(
    candidates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build one flat appendix table across candidate profiles."""
    out: List[Dict[str, Any]] = []
    for item in candidates:
        profile = item.get("profile") if "profile" in item else item
        if not isinstance(profile, dict):
            continue
        cid = profile.get("candidate_id") or "unknown"
        name = profile.get("candidate_name") or cid
        for row in build_source_audit_rows(profile.get("sources") or []):
            out.append({
                "candidate_id": cid,
                "candidate_name": name,
                **row,
            })
    return out


# --------------------------------------------------------------------------- #
# Compact source audit (one summary table instead of giant per-field rows)
# --------------------------------------------------------------------------- #

# (family label, default purpose note). Matched in order by name/url substrings.
_SOURCE_FAMILIES = [
    ("nearby search",  "Google Places Nearby Search", "Demand & accessibility signals"),
    ("text search",    "Google Places Text Search",   "Competitor discovery"),
    ("place details",  "Google Places Details",       "Competitor / anchor detail"),
    ("geocod",         "Google Geocoding",            "Coordinate resolution"),
    ("competitor website", "Competitor Website Fetch", "Competitor weakness signals"),
    ("census",         "Census ACS",                  "Economic context"),
    ("acs",            "Census ACS",                  "Economic context"),
    ("job posting",    "Job Postings CSV",            "B2B certification demand"),
    ("rent override",  "Rent Override",               "Commercial rent (manual)"),
    ("manual rent",    "Rent Override",               "Commercial rent (manual)"),
    ("bls",            "BLS / Labor Market",          "Labor-market context"),
    ("labor",          "BLS / Labor Market",          "Labor-market context"),
]


def _source_family(name: str, url: str) -> tuple:
    """Map a raw source record to a (family_label, purpose_note) pair."""
    label = (name or "").lower()
    for needle, family, purpose in _SOURCE_FAMILIES:
        if needle in label:
            return family, purpose
    # An http(s) record with no recognisable name is almost always a website
    # fetch — collapse all of these together instead of one row each.
    if url.startswith(("http://", "https://")) and (
        not label or label == "unknown"
    ):
        return "Competitor Website Fetch", "Competitor weakness signals"
    return (name or "Other source"), "Other"


# Map source family -> provider tag used in the cache session.
_FAMILY_TO_PROVIDER = {
    "Google Places Nearby Search": "google_places",
    "Google Places Text Search":   "google_places",
    "Google Places Details":       "google_places",
    "Google Geocoding":            "google_places",
    "Census ACS":                  "census",
    "BLS / Labor Market":          "bls_qcew",
}


def build_compact_source_audit(
    sources: Iterable[Dict[str, Any]],
    cache: Any = None,            # Optional[Cache]; typed Any to avoid circular import
    session_as_of: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Collapse many raw source records into one compact summary table.

    ``data_as_of`` lookup order, per row:
      1. ``session_as_of[provider]`` if supplied (preferred — survives
         report re-renders from saved JSON, where the live Cache is gone).
      2. ``cache.session_max_as_of(provider)`` if a live Cache is supplied.
      3. ``max(collected_at)`` across records in this family (fallback).
    """
    families: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for source in sources or []:
        name = str(source.get("name") or "unknown")
        url = str(source.get("url") or "")
        fields = [f for f in (source.get("fields") or []) if f]
        collected_at = str(source.get("collected_at") or "")
        family, purpose = _source_family(name, url)
        if family not in families:
            order.append(family)
            families[family] = {
                "source": family,
                "purpose": purpose,
                "records": 0,
                "_fields": set(),
                "_quality": set(),
                "_collected_at": [],
            }
        bucket = families[family]
        bucket["records"] += 1
        bucket["_fields"].update(fields)
        bucket["_quality"].add(source_quality(name, url))
        if collected_at:
            bucket["_collected_at"].append(collected_at)

    rows: List[Dict[str, Any]] = []
    for family in order:
        bucket = families[family]
        quality_set = sorted(q for q in bucket["_quality"] if q != "unknown")
        quality = quality_set[0] if len(quality_set) == 1 else (
            "mixed" if quality_set else "unknown"
        )
        field_count = len(bucket["_fields"])
        records = bucket["records"]
        if family == "Competitor Website Fetch":
            detail = f"{records} website(s) checked"
        elif field_count:
            detail = f"{records} record(s) · {field_count} field(s)"
        else:
            detail = f"{records} record(s)"
        # Determine data_as_of: prefer the cache session, fall back to the
        # max collected_at among this family's records.
        provider = _FAMILY_TO_PROVIDER.get(family)
        data_as_of = ""
        if session_as_of and provider:
            data_as_of = session_as_of.get(provider) or ""
        if not data_as_of and cache is not None and provider:
            data_as_of = cache.session_max_as_of(provider) or ""
        if not data_as_of and bucket["_collected_at"]:
            data_as_of = max(bucket["_collected_at"])
        rows.append({
            "source": family,
            "records": records,
            "field_count": field_count,
            "fields": sorted(bucket["_fields"]),
            "detail": detail,
            "quality": quality,
            "notes": bucket["purpose"],
            "data_as_of": data_as_of,
        })
    return rows


def build_compact_source_audit_for_candidates(
    candidates: Iterable[Dict[str, Any]],
    cache: Any = None,
    session_as_of: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []
    for item in candidates:
        profile = item.get("profile") if "profile" in item else item
        if not isinstance(profile, dict):
            continue
        flat.extend(profile.get("sources") or [])
    return build_compact_source_audit(flat, cache=cache,
                                       session_as_of=session_as_of)
