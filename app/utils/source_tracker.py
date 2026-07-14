"""
Source tracker — every data point a candidate carries should have a provenance
record so reports can cite where the number came from and the confidence
scorer can penalize missing/stale data.

Design: a `SourceTracker` is a lightweight container attached to a candidate
record. It accumulates:
  - sources: list of {name, url, collected_at, fields, notes}
  - missing_fields: set of field names we tried to fill but couldn't
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SourceRecord:
    name: str                       # e.g. "Google Places API" or "US Census ACS 5yr"
    url: str                        # citation URL (may be the API endpoint)
    collected_at: str               # ISO UTC timestamp
    fields: List[str]               # which fields this source populated
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "collected_at": self.collected_at,
            "fields": list(self.fields),
            "notes": self.notes,
        }


@dataclass
class SourceTracker:
    sources: List[SourceRecord] = field(default_factory=list)
    missing_fields: Set[str] = field(default_factory=set)

    def add(self, name: str, url: str, fields: Iterable[str], notes: str = "",
            collected_at: Optional[str] = None) -> None:
        self.sources.append(SourceRecord(
            name=name,
            url=url,
            collected_at=collected_at or utcnow_iso(),
            fields=list(fields),
            notes=notes,
        ))

    def mark_missing(self, *fields: str) -> None:
        for f in fields:
            self.missing_fields.add(f)

    @property
    def source_urls(self) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for s in self.sources:
            if s.url and s.url not in seen:
                seen.add(s.url)
                out.append(s.url)
        return out

    @property
    def source_names(self) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for s in self.sources:
            if s.name and s.name not in seen:
                seen.add(s.name)
                out.append(s.name)
        return out

    def as_dict(self) -> dict:
        return {
            "sources": [s.as_dict() for s in self.sources],
            "missing_fields": sorted(self.missing_fields),
        }
