"""Tests for the rent override template generator."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "fake-key-for-tests")

from scripts import generate_rent_template as grt


def _payload():
    return {
        "candidates": [
            {"profile": {"comparison_area": "Area A", "state": "CA",
                         "latitude": 37.3213, "longitude": -121.9478}},
            {"profile": {"comparison_area": "Area B", "state": "CA",
                         "latitude": 37.5519, "longitude": -121.9780}},
            # Duplicate coordinates of Area A — should collapse.
            {"profile": {"comparison_area": "Area A copy", "state": "CA",
                         "latitude": 37.3213, "longitude": -121.9478}},
        ]
    }


def test_generated_rows_are_blank_and_deduped():
    rows = grt._generated_rows(_payload(), radius_miles=2.0)
    assert len(rows) == 2  # duplicate coordinates collapsed
    for row in rows:
        assert row["rent_per_sqft_annual"] == ""   # never invents a value
        assert row["radius_miles"] == "2"
        assert row["city"] == ""                   # matches purely on coords


def test_merge_preserves_already_filled_rows():
    existing = [{
        "city": "", "state": "CA",
        "latitude": "37.32130", "longitude": "-121.94780",
        "radius_miles": "2", "rent_per_sqft_annual": "39.5",
        "source_url": "https://example.com/rent", "notes": "broker comp",
    }]
    rows = grt.build_rows(_payload(), radius_miles=2.0,
                          existing_filled=existing, merge_distance_miles=0.3)
    area_a = [r for r in rows if r["latitude"].startswith("37.321")][0]
    # The filled rent for Area A survives regeneration.
    assert area_a["rent_per_sqft_annual"] == "39.5"
    # Area B is still a blank row needing a value.
    area_b = [r for r in rows if r["latitude"].startswith("37.551")][0]
    assert area_b["rent_per_sqft_annual"] == ""


def test_is_same_location_uses_distance_threshold():
    near = {"latitude": "37.3213", "longitude": "-121.9478"}
    same = {"latitude": "37.3214", "longitude": "-121.9479"}   # ~0.01 mi
    far = {"latitude": "37.5519", "longitude": "-121.9780"}    # ~15 mi
    assert grt._is_same_location(near, same, 0.3) is True
    assert grt._is_same_location(near, far, 0.3) is False


def test_run_writes_csv(tmp_path: Path):
    payload_path = tmp_path / "scored.json"
    payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "rent_overrides.csv"
    rc = grt.run(["--input", str(payload_path), "--output", str(out),
                  "--radius-miles", "2"])
    assert rc == 0
    with open(out, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert all(r["rent_per_sqft_annual"] == "" for r in rows)
