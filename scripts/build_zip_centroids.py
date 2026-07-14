"""
Build / refresh ``data/reference/zip_centroids.csv`` from the public-domain
Census ZIP Code Tabulation Area (ZCTA) Gazetteer.

Why this exists
---------------
ZIP-level demand resolution (:mod:`app.scoring.zip_demand`) can only match a
demand ZIP by radius when that ZIP's centroid is in the reference file. The
file shipped as a hand-typed 10-ZIP South-Bay fixture, so any run whose
held-class demand reached a ZIP outside that list silently lost those ZIPs
from ``exact_plus_radius`` matching. This script removes that gap at the
source: it downloads the authoritative Census Gazetteer (no API key, no card —
public domain) and writes ``zip,lat,lng`` rows.

The Gazetteer ZCTA national file is a tab-delimited table whose columns
include ``GEOID`` (the 5-digit ZCTA), ``INTPTLAT`` and ``INTPTLONG`` (the
internal-point latitude/longitude). The parser is column-name driven, so it
tolerates the extra land/water-area columns the file also carries and the
trailing whitespace Census leaves on the ``INTPTLONG`` header.

Examples
--------
    # National refresh (≈33k ZCTAs):
    python scripts/build_zip_centroids.py

    # Keep the file small: only the ZIPs your current Enrollware export needs:
    python scripts/build_zip_centroids.py --only-demand-zips

    # Add new ZIPs without dropping ones you already have:
    python scripts/build_zip_centroids.py --only-demand-zips --merge

    # Parse a file you downloaded yourself (offline / pinned vintage):
    python scripts/build_zip_centroids.py --from-file ~/2024_Gaz_zcta_national.txt
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors import enrollware
from app.scoring import zip_demand
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

DEFAULT_YEAR = 2024
# Census distributes the ZCTA gazetteer as a zip per vintage.
GAZETTEER_URL_TEMPLATE = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "{year}_Gazetteer/{year}_Gaz_zcta_national.zip"
)


# --------------------------------------------------------------------------- #
# Fetch + parse (kept as small pure-ish functions so tests skip the network)
# --------------------------------------------------------------------------- #

def gazetteer_url(year: int) -> str:
    return GAZETTEER_URL_TEMPLATE.format(year=year)


def fetch_gazetteer_text(url: str, *, timeout: int = 120) -> str:
    """Download the Gazetteer file and return its decoded text.

    The endpoint serves either a ``.zip`` (the official distribution) or a raw
    ``.txt`` (a mirror). Both are handled. Census files are latin-1; decoding
    that way never raises on the occasional non-ASCII byte.
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.content
    if url.lower().endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError(f"no .txt member in gazetteer zip at {url}")
            return zf.read(names[0]).decode("latin-1")
    return data.decode("latin-1")


def parse_gazetteer(text: str) -> Dict[str, Tuple[float, float]]:
    """Parse Gazetteer text into ``{zip: (lat, lng)}``.

    Column-name driven: locates GEOID / INTPTLAT / INTPTLONG by header so the
    extra area columns and Census's trailing-whitespace headers don't matter.
    Rows with a non-5-digit GEOID or unparseable coordinates are skipped — the
    same "never invent a centroid" stance as the rest of the pipeline.
    """
    lines = text.splitlines()
    if not lines:
        return {}
    header = [h.strip().upper() for h in lines[0].split("\t")]
    try:
        i_zip = header.index("GEOID")
        i_lat = header.index("INTPTLAT")
        i_lng = header.index("INTPTLONG")
    except ValueError as exc:
        raise ValueError(
            f"gazetteer header missing GEOID/INTPTLAT/INTPTLONG: {header}"
        ) from exc
    out: Dict[str, Tuple[float, float]] = {}
    need = max(i_zip, i_lat, i_lng)
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) <= need:
            continue
        z = cols[i_zip].strip().zfill(5)
        if len(z) != 5 or not z.isdigit():
            continue
        try:
            lat = float(cols[i_lat].strip())
            lng = float(cols[i_lng].strip())
        except ValueError:
            continue
        out[z] = (lat, lng)
    return out


def parse_gazetteer_records(text: str) -> Dict[str, Dict[str, float]]:
    """Like :func:`parse_gazetteer` but also keep land area (sq mi).

    Returns ``{zip: {"lat", "lng", "land_sqmi"}}``. Land area powers the
    national modeled layer's population-density signal. ``land_sqmi`` is 0.0
    when the column is absent/unparseable (density then drops out downstream —
    never invented). Reuses the same column-name-driven, skip-bad-rows stance.
    """
    lines = text.splitlines()
    if not lines:
        return {}
    header = [h.strip().upper() for h in lines[0].split("\t")]
    try:
        i_zip = header.index("GEOID")
        i_lat = header.index("INTPTLAT")
        i_lng = header.index("INTPTLONG")
    except ValueError as exc:
        raise ValueError(
            f"gazetteer header missing GEOID/INTPTLAT/INTPTLONG: {header}"
        ) from exc
    i_area = header.index("ALAND_SQMI") if "ALAND_SQMI" in header else None
    out: Dict[str, Dict[str, float]] = {}
    need = max(i_zip, i_lat, i_lng, i_area or 0)
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) <= need:
            continue
        z = cols[i_zip].strip().zfill(5)
        if len(z) != 5 or not z.isdigit():
            continue
        try:
            lat = float(cols[i_lat].strip())
            lng = float(cols[i_lng].strip())
        except ValueError:
            continue
        land_sqmi = 0.0
        if i_area is not None:
            try:
                land_sqmi = float(cols[i_area].strip())
            except ValueError:
                land_sqmi = 0.0
        out[z] = {"lat": lat, "lng": lng, "land_sqmi": land_sqmi}
    return out


# --------------------------------------------------------------------------- #
# Demand-ZIP filter + write
# --------------------------------------------------------------------------- #

def demand_zips(
    enroll_path: Optional[Path] = None,
    locations_path: Optional[Path] = None,
) -> Set[str]:
    """The set of ZIPs carrying held-class demand in the Enrollware export.

    These are exactly the ZIPs that need a centroid for radius matching to be
    complete. Empty when no export (or no ZIP-resolved demand) is available.
    """
    records = enrollware.load_records(enroll_path, locations_path=locations_path)
    return set(zip_demand.aggregate_zip_demand(records).keys())


def write_centroids(path: Path,
                    centroids: Dict[str, Tuple[float, float]]) -> None:
    """Write ``zip,lat,lng`` rows sorted by ZIP. Round-trips through
    :func:`app.scoring.zip_demand.load_zip_centroids`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["zip", "lat", "lng"])
        for z in sorted(centroids):
            lat, lng = centroids[z]
            w.writerow([z, f"{lat:.5f}", f"{lng:.5f}"])


def _restrict(centroids: Dict[str, Tuple[float, float]],
              keep: Iterable[str]) -> Dict[str, Tuple[float, float]]:
    keep_set = set(keep)
    return {z: c for z, c in centroids.items() if z in keep_set}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=DEFAULT_YEAR,
                    help=f"Census Gazetteer vintage (default {DEFAULT_YEAR}).")
    ap.add_argument("--url", default="",
                    help="Override the download URL (defaults to the Census "
                         "ZCTA gazetteer for --year).")
    ap.add_argument("--from-file", default="",
                    help="Parse a local Gazetteer .txt/.zip instead of "
                         "downloading (offline / pinned vintage).")
    ap.add_argument("--output", default="",
                    help="Output CSV (default data/reference/zip_centroids.csv).")
    ap.add_argument("--only-demand-zips", action="store_true",
                    help="Keep only ZIPs present in the Enrollware demand "
                         "export — keeps the committed file small.")
    ap.add_argument("--merge", action="store_true",
                    help="Union with the existing file instead of overwriting "
                         "(new coordinates win on conflict).")
    ap.add_argument("--enrollware-file", default="",
                    help="Explicit Enrollware classes export (for "
                         "--only-demand-zips).")
    ap.add_argument("--enrollware-locations-file", default="",
                    help="Explicit Enrollware locations export (for "
                         "--only-demand-zips).")
    args = ap.parse_args(argv)

    out_path = (Path(args.output) if args.output
                else zip_demand.ZIP_CENTROIDS_FILE)

    # 1. Source the full centroid table (download or local file).
    if args.from_file:
        src = Path(args.from_file)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                if not names:
                    logger.error(f"no .txt member in {src}")
                    return 1
                text = zf.read(names[0]).decode("latin-1")
        else:
            text = src.read_text(encoding="latin-1")
        logger.info(f"Parsing local gazetteer {src}")
    else:
        url = args.url or gazetteer_url(args.year)
        logger.info(f"Downloading Census ZCTA gazetteer: {url}")
        try:
            text = fetch_gazetteer_text(url)
        except Exception as exc:
            logger.error(f"download failed: {exc}")
            return 1

    try:
        centroids = parse_gazetteer(text)
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    logger.info(f"Parsed {len(centroids)} ZCTA centroid(s).")
    if not centroids:
        logger.error("no centroids parsed — aborting (file would be emptied).")
        return 1

    # 2. Optionally restrict to the demand footprint.
    if args.only_demand_zips:
        enroll_path = (Path(args.enrollware_file)
                       if args.enrollware_file else None)
        loc_path = (Path(args.enrollware_locations_file)
                    if args.enrollware_locations_file else None)
        wanted = demand_zips(enroll_path, loc_path)
        if not wanted:
            logger.error(
                "--only-demand-zips: the Enrollware export resolved no demand "
                "ZIPs (missing export or no locations join) — aborting rather "
                "than writing an empty file.")
            return 1
        kept = _restrict(centroids, wanted)
        missing = sorted(wanted - kept.keys())
        if missing:
            logger.warning(
                f"{len(missing)} demand ZIP(s) absent from the gazetteer "
                f"(out of range / non-ZCTA): {', '.join(missing[:10])}"
                + ("" if len(missing) <= 10 else " ..."))
        centroids = kept
        logger.info(f"Restricted to {len(centroids)} demand ZIP(s).")

    # 3. Optionally merge with what's already committed.
    if args.merge:
        existing = zip_demand.load_zip_centroids(out_path)
        merged = dict(existing)
        merged.update(centroids)   # new coordinates win on conflict
        logger.info(
            f"Merged with {len(existing)} existing row(s) -> {len(merged)} total.")
        centroids = merged

    write_centroids(out_path, centroids)
    logger.info(f"Wrote {len(centroids)} centroid(s) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
