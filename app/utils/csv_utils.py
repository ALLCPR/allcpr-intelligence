"""Shared CSV / DataFrame helpers."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from app.utils.logging_utils import get_logger

logger = get_logger(__name__)


def load_csv(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    logger.info(f"Loaded {len(df)} rows from {path}")
    return df


def save_csv(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"Saved {len(df)} rows -> {path}")


def write_dicts_csv(rows: List[dict], path: Path,
                    fieldnames: Optional[List[str]] = None) -> None:
    """Write a list of dicts to a CSV. Creates parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        logger.info(f"Wrote empty CSV -> {path}")
        return
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Saved {len(rows)} rows -> {path}")


def load_lines(path: Path) -> List[str]:
    """Load non-empty, non-comment lines from a plain text file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def unique(seq: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in seq:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
