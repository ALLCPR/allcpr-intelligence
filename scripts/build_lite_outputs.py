#!/usr/bin/env python3
"""Build memory-safe dashboard payloads from the full national output.

This script is intentionally offline-only. It may load the existing full
national JSON locally, then writes:

* data/processed/national_demand_lite.json
* data/processed/national_demand_lite.json.gz
* data/processed/zip_details.jsonl + zip_details_index.json, when --details is enabled
* data/processed/zip_details/<zip>.json(.gz), when --split-details is enabled

The web service serves these prebuilt files and does not rebuild them at
startup or inside request handlers.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.reports.commercial_validation import load_commercial_summaries
from app.scoring.site_priority_score import annotate_site_priority_scores

PROCESSED_DIR = ROOT / "data" / "processed"
DEFAULT_INPUTS = (
    PROCESSED_DIR / "national_demand_enriched.json",
    PROCESSED_DIR / "national_demand.json",
)
LITE_FIELDS = (
    "zip",
    "lat",
    "lon",
    "overall_score",
    "aha_bls",
    "arc_bls",
    "arc_cpr",
    "tier",
    "data_confidence",
    "confidence",
    "validation_score",
    "validation_tier",
)


def gzip_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as f_in:
        with gzip.open(dst, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)


def _num(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    try:
        out = float(value)
    except (TypeError, ValueError):
        return value
    if out != out:
        return None
    return round(out, 6)


def lite_row(row: Dict[str, Any]) -> Dict[str, Any]:
    bls = row.get("bls_demand")
    cpr = row.get("cpr_demand")
    out = {
        "zip": str(row.get("zip", "")).zfill(5),
        "lat": _num(row.get("lat")),
        "lon": _num(row.get("lon", row.get("lng"))),
        "overall_score": _num(row.get("overall_score", row.get("overall"))),
        "aha_bls": _num(row.get("aha_bls", bls)),
        "arc_bls": _num(row.get("arc_bls", bls)),
        "arc_cpr": _num(row.get("arc_cpr", cpr)),
        "tier": row.get("tier"),
    }
    for key in ("data_confidence", "confidence", "validation_score", "validation_tier"):
        if row.get(key) is not None:
            out[key] = _num(row[key])
    return {key: out[key] for key in LITE_FIELDS if out.get(key) is not None}


def build_lite_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = payload.get("rows") or []
    return {
        "generated_at": payload.get("generated_at"),
        "layer": payload.get("layer", "modeled_national_demand"),
        "tier": "lite",
        "acs_vintage": payload.get("acs_vintage"),
        "acs_label": payload.get("acs_label"),
        "zip_count": len(rows),
        "methodology": payload.get("methodology"),
        "enriched_zip_count": payload.get("enriched_zip_count", 0),
        "enrichment_summary": payload.get("enrichment_summary"),
        "rows": [lite_row(row) for row in rows],
    }


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def write_zip_details(
    rows: list[Dict[str, Any]],
    details_dir: Path,
    *,
    gzip_details: bool,
    clean: bool,
    commercial_by_zip: Dict[str, Dict[str, Any]] | None = None,
) -> int:
    if clean and details_dir.exists():
        shutil.rmtree(details_dir)
    details_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for row in rows:
        zip_code = str(row.get("zip", "")).zfill(5)
        if not zip_code or not zip_code.isdigit():
            continue
        if commercial_by_zip and zip_code in commercial_by_zip:
            row = dict(row)
            row["commercial"] = commercial_by_zip[zip_code]
        row = annotate_site_priority_scores(row)
        raw_path = details_dir / f"{zip_code}.json"
        write_json(raw_path, row)
        if gzip_details:
            gzip_file(raw_path, details_dir / f"{zip_code}.json.gz")
            raw_path.unlink()
        count += 1
    return count


def write_jsonl_details(
    rows: list[Dict[str, Any]],
    jsonl_path: Path,
    index_path: Path,
    commercial_by_zip: Dict[str, Dict[str, Any]] | None = None,
) -> int:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    index: Dict[str, int] = {}
    count = 0
    with jsonl_path.open("wb") as fh:
        for row in rows:
            zip_code = str(row.get("zip", "")).zfill(5)
            if not zip_code or not zip_code.isdigit():
                continue
            if commercial_by_zip and zip_code in commercial_by_zip:
                row = dict(row)
                row["commercial"] = commercial_by_zip[zip_code]
            row = annotate_site_priority_scores(row)
            index[zip_code] = fh.tell()
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            fh.write(line.encode("utf-8"))
            fh.write(b"\n")
            count += 1
    write_json(index_path, index)
    return count


def default_input_path() -> Path:
    for path in DEFAULT_INPUTS:
        if path.exists():
            return path
    return DEFAULT_INPUTS[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=default_input_path())
    parser.add_argument("--lite-output", type=Path, default=PROCESSED_DIR / "national_demand_lite.json")
    parser.add_argument("--lite-gzip-output", type=Path, default=PROCESSED_DIR / "national_demand_lite.json.gz")
    parser.add_argument("--details-dir", type=Path, default=PROCESSED_DIR / "zip_details")
    parser.add_argument("--details-jsonl", type=Path, default=PROCESSED_DIR / "zip_details.jsonl")
    parser.add_argument("--details-index", type=Path, default=PROCESSED_DIR / "zip_details_index.json")
    parser.add_argument("--details", action="store_true", help="Write seekable full detail JSONL + index.")
    parser.add_argument("--split-details", action="store_true", help="Also write per-ZIP full detail files.")
    parser.add_argument("--gzip-details", action="store_true", help="Compress per-ZIP detail files.")
    parser.add_argument("--clean-details", action="store_true", help="Remove old detail files before writing.")
    args = parser.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input not found: {args.input}")

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    lite = build_lite_payload(payload)
    write_json(args.lite_output, lite)
    gzip_file(args.lite_output, args.lite_gzip_output)
    commercial_by_zip = load_commercial_summaries()

    detail_count = 0
    if args.details:
        detail_count = write_jsonl_details(
            payload.get("rows") or [],
            args.details_jsonl,
            args.details_index,
            commercial_by_zip,
        )
    if args.split_details:
        detail_count = write_zip_details(
            payload.get("rows") or [],
            args.details_dir,
            gzip_details=args.gzip_details,
            clean=args.clean_details,
            commercial_by_zip=commercial_by_zip,
        )

    print(f"Wrote {args.lite_output} ({len(lite['rows'])} rows)")
    print(f"Wrote {args.lite_gzip_output}")
    if args.details:
        print(f"Wrote {detail_count} ZIP detail rows to {args.details_jsonl}")
        print(f"Wrote detail index to {args.details_index}")
    if args.split_details:
        print(f"Wrote {detail_count} split ZIP detail files to {args.details_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
