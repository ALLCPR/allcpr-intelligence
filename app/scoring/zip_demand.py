"""
ZIP-level demand: aggregation, 0..100 demand score, bounded score adjustment,
confidence modifier, and candidate->ZIP resolution.

Independent signal by design (see the spec): NOT in SCORE_WEIGHTS, never moves
ranking. ``site_score.score_profile`` applies the bounded adjustment to produce
a display-only ``final_score``; ranking and tiers stay on ``area_score`` until
the signal is validated across enough cities.

Honesty rules: aggregation runs on held classes only; unknown fields stay
None; a candidate with no matching ZIP history gets a neutral (zero) effect.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.collectors.enrollware import EnrollwareClassRecord, held_classes
from app.config import RAW_DIR
from app.scoring.course_types import (
    AHA_BLS,
    ARC_BLS,
    ARC_CPR,
    BUCKET_LABELS,
    REPORT_BUCKETS,
    demand_strength_category,
    to_demand_bucket,
)
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# Default radius for "nearby ZIPs" — matches the pipeline's counts_5mi basis.
DEFAULT_RADIUS_MILES = 5.0

# Reference average students/class used to normalize avg-enrollment when the
# caller does not thread in the ALLCPR overall average (e.g. tiny fixtures).
DEFAULT_REFERENCE_AVG = 6.0

# data/reference/zip_centroids.csv — committed, public-domain Census ZCTA
# centroids. Missing file => radius matching silently disabled.
ZIP_CENTROIDS_FILE = RAW_DIR.parent / "reference" / "zip_centroids.csv"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

@dataclass
class BucketStat:
    classes: int = 0
    students: int = 0
    avg_students: Optional[float] = None
    capacity: Optional[int] = None       # summed over rows with known capacity
    fill_rate: Optional[float] = None    # percent 0..100, this channel only

    def to_dict(self) -> Dict[str, Any]:
        return {"classes": self.classes, "students": self.students,
                "avg_students": self.avg_students,
                "capacity": self.capacity, "fill_rate": self.fill_rate}


@dataclass
class ZipDemandProfile:
    zip: str
    total_classes: int = 0
    total_students: Optional[int] = None
    average_students_per_class: Optional[float] = None
    total_capacity: Optional[int] = None
    fill_rate: Optional[float] = None          # percent 0..100, capacity-known rows
    earliest_class_date: Optional[str] = None  # ISO YYYY-MM-DD
    latest_class_date: Optional[str] = None    # ISO YYYY-MM-DD
    buckets: Dict[str, BucketStat] = field(default_factory=dict)
    cities: List[str] = field(default_factory=list)   # normalized, for fallback

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zip": self.zip,
            "total_classes": self.total_classes,
            "total_students": self.total_students,
            "average_students_per_class": self.average_students_per_class,
            "total_capacity": self.total_capacity,
            "fill_rate": self.fill_rate,
            "earliest_class_date": self.earliest_class_date,
            "latest_class_date": self.latest_class_date,
            "buckets": {k: b.to_dict() for k, b in self.buckets.items()},
            "cities": list(self.cities),
        }


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def aggregate_zip_demand(
    records: List[EnrollwareClassRecord],
) -> Dict[str, ZipDemandProfile]:
    """Per-ZIP demand profiles over HELD classes only.

    Records without a ZIP are skipped (the join could not resolve one — we
    never guess). OTHER-bucket classes count in the ZIP totals but are not a
    charted channel.
    """
    held = held_classes(records)   # single call — both passes see one cutoff
    out: Dict[str, ZipDemandProfile] = {}
    for r in held:
        if not r.zip:
            continue
        p = out.get(r.zip)
        if p is None:
            p = ZipDemandProfile(
                zip=r.zip,
                buckets={b: BucketStat() for b in REPORT_BUCKETS},
            )
            out[r.zip] = p
        p.total_classes += 1
        if r.enrolled is not None:
            p.total_students = (p.total_students or 0) + r.enrolled
        if r.capacity is not None and r.capacity > 0:
            p.total_capacity = (p.total_capacity or 0) + r.capacity
        if r.date and (p.earliest_class_date is None
                       or r.date < p.earliest_class_date):
            p.earliest_class_date = r.date
        if r.date and (p.latest_class_date is None or r.date > p.latest_class_date):
            p.latest_class_date = r.date
        city_n = _norm(r.city)
        if city_n and city_n not in p.cities:
            p.cities.append(city_n)

        bucket = to_demand_bucket(r.course_type, r.class_name)
        if bucket in p.buckets and r.enrolled is not None:
            b = p.buckets[bucket]
            b.classes += 1
            b.students += r.enrolled

    # Derived fields (averages) once per ZIP; fill is a dedicated honest pass
    # over paired enrolled+capacity rows (matches historical _fill_rate basis).
    for p in out.values():
        if p.total_students is not None and p.total_classes > 0:
            p.average_students_per_class = round(
                p.total_students / p.total_classes, 2)
        for b in p.buckets.values():
            if b.classes > 0:
                b.avg_students = round(b.students / b.classes, 2)
    _attach_fill_rates(held, out)
    return out


def _attach_fill_rates(records: List[EnrollwareClassRecord],
                       out: Dict[str, ZipDemandProfile]) -> None:
    """fill_rate per ZIP and per charted bucket, over held rows where BOTH
    enrolled and capacity are known (matches historical _fill_rate basis)."""
    zip_pairs: Dict[str, Tuple[int, int]] = {}
    bucket_pairs: Dict[Tuple[str, str], Tuple[int, int]] = {}
    for r in records:
        if not r.zip or r.zip not in out:
            continue
        if r.enrolled is None or r.capacity is None or r.capacity <= 0:
            continue
        filled, cap = zip_pairs.get(r.zip, (0, 0))
        zip_pairs[r.zip] = (filled + r.enrolled, cap + r.capacity)
        bucket = to_demand_bucket(r.course_type, r.class_name)
        if bucket in out[r.zip].buckets:
            bf, bc = bucket_pairs.get((r.zip, bucket), (0, 0))
            bucket_pairs[(r.zip, bucket)] = (bf + r.enrolled, bc + r.capacity)
    for z, (filled, cap) in zip_pairs.items():
        if cap > 0:
            out[z].fill_rate = round(100.0 * filled / cap, 1)
    for (z, bucket), (filled, cap) in bucket_pairs.items():
        if cap > 0:
            stat = out[z].buckets[bucket]
            stat.capacity = cap
            stat.fill_rate = round(100.0 * filled / cap, 1)


# --------------------------------------------------------------------------- #
# Demand score (0..100) + bounded adjustment + confidence modifier
# --------------------------------------------------------------------------- #

# Class-count saturation: log-damped so a handful of classes registers but
# volume saturates around this many charted classes.
_VOLUME_SATURATION_CLASSES = 40
# Recency decay horizon in months (relative to the latest export date).
_RECENCY_HORIZON_MONTHS = 18.0


def _months_between(earlier: Optional[str], later: Optional[str]) -> Optional[float]:
    """Whole-ish months between two ISO dates; None when either is missing."""
    if not earlier or not later:
        return None
    try:
        a = datetime.strptime(earlier[:10], "%Y-%m-%d")
        b = datetime.strptime(later[:10], "%Y-%m-%d")
    except ValueError:
        return None
    raw_months = (b - a).days / 30.44
    if raw_months < 0:
        logger.warning(
            f"zip_demand: date inversion — latest class {earlier!r} is after "
            f"the export anchor {later!r}; clamping recency gap to 0."
        )
    return max(0.0, raw_months)


def compute_zip_demand_score(
    profile: Optional[ZipDemandProfile],
    reference_avg: Optional[float] = None,
    latest_export_date: Optional[str] = None,
) -> Optional[float]:
    """0..100 demand score: f(weighted class count, avg students, fill, recency).

    Fill rate is a MULTIPLICATIVE damper (0.5x..1.0x): high class volume with
    empty seats reads as weak demand — the spec's explicit guard. Unknown fill
    is neutral (0.5 -> 0.75x), never fatal.

    ``reference_avg`` is the ALLCPR overall average students/class, threaded in
    by the caller (mirrors historical_performance's reference_avg). Defaults to
    DEFAULT_REFERENCE_AVG. ``latest_export_date`` anchors recency to the export,
    not the wall clock — consistent with _recent_activity's basis. When no
    anchor is provided, recency is neutral (0.5).
    """
    if profile is None or profile.total_classes <= 0:
        return None
    ref = reference_avg if reference_avg and reference_avg > 0 else DEFAULT_REFERENCE_AVG

    charted_classes = sum(
        profile.buckets[b].classes for b in REPORT_BUCKETS
        if b in profile.buckets
    )
    charted_students = sum(
        profile.buckets[b].students for b in REPORT_BUCKETS
        if b in profile.buckets
    )
    volume = min(1.0, math.log1p(charted_classes)
                 / math.log1p(_VOLUME_SATURATION_CLASSES))

    # Average uses the charted channels ONLY: OTHER-bucket enrollment (ACLS,
    # PALS, house brand) must not inflate the three-channel demand signal, and
    # volume above is already charted-only — keep the two terms consistent.
    charted_avg = (charted_students / charted_classes) if charted_classes else 0.0
    avg_c = min(1.0, charted_avg / ref) if charted_avg > 0 else 0.0

    months = (_months_between(profile.latest_class_date, latest_export_date)
              if latest_export_date else None)
    if months is None:
        recency = 0.5            # undated history or no export anchor: neutral, not penalized
    else:
        recency = max(0.0, min(1.0, 1.0 - months / _RECENCY_HORIZON_MONTHS))

    raw = 0.45 * volume + 0.30 * avg_c + 0.25 * recency

    # Fill damper: finiteness checked BEFORE the clamp — min(1.0, nan) returns
    # 1.0, so a post-clamp guard would silently read a NaN fill as fully booked.
    raw_fill = profile.fill_rate
    if (isinstance(raw_fill, (int, float)) and not isinstance(raw_fill, bool)
            and math.isfinite(raw_fill)):
        fill01 = max(0.0, min(1.0, raw_fill / 100.0))
    else:
        fill01 = 0.5            # unknown / non-finite fill = neutral
    damper = 0.5 + 0.5 * fill01   # fill 0% halves the score; 100% leaves it

    return round(max(0.0, min(100.0, 100.0 * raw * damper)), 1)


def compute_score_adjustment(zip_demand_score: Any) -> float:
    """Bounded correction: ((score - 50) / 50) * 5, capped to [-5, +5]."""
    try:
        s = float(zip_demand_score)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(s):
        return 0.0
    adj = ((s - 50.0) / 50.0) * 5.0
    return round(max(-5.0, min(5.0, adj)), 2)


def compute_confidence_modifier(
    profile: Optional[ZipDemandProfile],
    latest_export_date: Optional[str] = None,
) -> float:
    """Confidence effect in [-10, +10]. ZIP demand moves confidence more than
    score: strong/recent/full history earns trust; sparse/stale/empty erodes
    it. NO matching history is neutral 0 — absence is not evidence here (the
    pipeline-wide "no history = neutral" stance).
    """
    if profile is None or profile.total_classes <= 0:
        return 0.0
    mod = 0.0
    # Volume of held classes.
    if profile.total_classes >= 20:
        mod += 4.0
    elif profile.total_classes >= 8:
        mod += 2.0
    else:
        mod -= 4.0
    # Fill rate.
    if isinstance(profile.fill_rate, (int, float)):
        if profile.fill_rate >= 70:
            mod += 3.0
        elif profile.fill_rate < 40:
            mod -= 3.0
    # Recency vs the export's latest date.
    months = _months_between(profile.latest_class_date, latest_export_date)
    if months is not None:
        if months <= 6:
            mod += 3.0
        elif months > 18:
            mod -= 3.0
    return round(max(-10.0, min(10.0, mod)), 1)


# --------------------------------------------------------------------------- #
# Candidate ZIP parsing, centroid loading, candidate->ZIP resolution
# --------------------------------------------------------------------------- #

# ZIP at the END of an address tail (optionally ZIP+4). Trailing-anchored so a
# 5-digit street number never wins.
_ZIP_TAIL_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_ZIP_ANY_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def parse_zip(text: Any) -> Optional[str]:
    """Pull the candidate's 5-digit ZIP out of a formatted address.

    Prefers a trailing ZIP (the Google formatted_address shape: '..., CA
    95112' or '..., CA 95112, USA'); falls back to the LAST 5-digit token so a
    leading street number never masquerades as a ZIP.
    """
    s = str(text or "").strip()
    if not s:
        return None
    s = re.sub(r",?\s*(usa|united states)\.?$", "", s, flags=re.I).strip()
    m = _ZIP_TAIL_RE.search(s)
    if m:
        return m.group(1)
    matches = _ZIP_ANY_RE.findall(s)
    # Fallback: last 5-digit run, but never a lone street number (a bare ZIP
    # string is accepted by the tail branch above, not here).
    return matches[-1] if len(matches) > 1 else None


def load_zip_centroids(
    path: Optional[Path] = None,
) -> Dict[str, Tuple[float, float]]:
    """zip -> (lat, lng) from data/reference/zip_centroids.csv.

    Missing/unreadable file => {} and radius matching is silently disabled
    (exact-ZIP and city fallback still work) — graceful, like every other
    optional data file in this pipeline.
    """
    target = Path(path) if path is not None else ZIP_CENTROIDS_FILE
    if not target.exists():
        return {}
    out: Dict[str, Tuple[float, float]] = {}
    try:
        with open(target, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                z = str(row.get("zip") or "").strip()
                try:
                    lat, lng = float(row["lat"]), float(row["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                if re.fullmatch(r"\d{5}", z):
                    out[z] = (lat, lng)
    except Exception as exc:
        logger.warning(f"zip_demand: failed to read centroids {target}: {exc}")
        return {}
    return out


# --------------------------------------------------------------------------- #
# Centroid coverage audit
# --------------------------------------------------------------------------- #
#
# Radius matching (``exact_plus_radius``) can only *see* a demand ZIP whose
# centroid is in the reference file. A demand ZIP that is absent is silently
# dropped from every candidate's nearby set — the resolver never measures a
# distance it can't compute. That degradation is documented as graceful, but
# it was also invisible. This audit makes it visible: it answers "which of the
# ZIPs that actually carry held-class demand are missing a centroid?" so a
# maintainer knows to refresh the file (scripts/build_zip_centroids.py).

@dataclass
class CentroidCoverage:
    """How well ``zip_centroids.csv`` covers the demand ZIPs in a run."""
    total_demand_zips: int
    covered_zips: List[str]
    missing_zips: List[str]
    centroids_present: bool   # a non-empty centroid file was loaded

    @property
    def coverage_pct(self) -> Optional[float]:
        """Percent of demand ZIPs with a centroid; None when there is no
        ZIP-resolved demand to measure against."""
        if self.total_demand_zips == 0:
            return None
        return round(100.0 * len(self.covered_zips) / self.total_demand_zips, 1)

    @property
    def radius_matching_usable(self) -> bool:
        """True only when a centroid file is present AND at least one demand
        ZIP is covered — otherwise ``exact_plus_radius`` can never fire."""
        return self.centroids_present and bool(self.covered_zips)

    def summary(self) -> str:
        """One human line for logs / healthcheck detail."""
        if not self.centroids_present:
            return (f"no centroid file — radius matching disabled "
                    f"({self.total_demand_zips} demand ZIP(s))")
        if self.total_demand_zips == 0:
            return "no ZIP-resolved demand to audit"
        if not self.missing_zips:
            return f"all {self.total_demand_zips} demand ZIP(s) have centroids"
        preview = ", ".join(self.missing_zips[:10])
        more = ("" if len(self.missing_zips) <= 10
                else f" (+{len(self.missing_zips) - 10} more)")
        return (f"{len(self.missing_zips)}/{self.total_demand_zips} demand "
                f"ZIP(s) missing centroids: {preview}{more}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_demand_zips": self.total_demand_zips,
            "covered_zips": list(self.covered_zips),
            "missing_zips": list(self.missing_zips),
            "centroids_present": self.centroids_present,
            "coverage_pct": self.coverage_pct,
            "radius_matching_usable": self.radius_matching_usable,
        }


def audit_centroid_coverage(
    demand_by_zip: Mapping[str, ZipDemandProfile],
    centroids: Mapping[str, Tuple[float, float]],
) -> CentroidCoverage:
    """Compare the ZIPs that carry demand against the centroid file. Pure —
    no logging, no I/O — so the pipeline, the healthcheck, and tests can all
    share one definition of "covered"."""
    demand_zips = sorted(demand_by_zip)
    covered = [z for z in demand_zips if z in centroids]
    missing = [z for z in demand_zips if z not in centroids]
    return CentroidCoverage(
        total_demand_zips=len(demand_zips),
        covered_zips=covered,
        missing_zips=missing,
        centroids_present=bool(centroids),
    )


# --------------------------------------------------------------------------- #
# Report-wide ZIP demand dataset (for the HTML visualization layer)
# --------------------------------------------------------------------------- #
#
# The per-candidate ``zip_demand`` payload answers "what demand does THIS site
# see"; this answers "what does demand look like across every ZIP in the run".
# It is the single, JSON-safe source the report table, scatter chart, and
# centroid map all draw from — so they can never disagree about a ZIP's score,
# class count, or centroid status. Pure (no I/O); the pipeline computes it once
# and stashes it in the report context.


def _ols(xs: List[float], ys: List[float]) -> Optional[Dict[str, float]]:
    """Least-squares fit of ``y = slope*x + intercept`` with R².

    Returns None when there are fewer than 3 points or x has no spread (a line
    would be meaningless / undefined). Keeps the report honest: no trend line
    is drawn unless one can actually be fit.
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    # R² = explained / total; syy==0 (all y equal) => a flat line explains all.
    r2 = 1.0 if syy < 1e-9 else max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))
    return {
        "slope": round(slope, 4),
        "intercept": round(intercept, 4),
        "r_squared": round(r2, 4),
    }


def _inclusive_month_span(
    start_date: Optional[str],
    end_date: Optional[str],
) -> Optional[int]:
    """Inclusive calendar-month span between two ISO dates.

    A single dated class month is a valid one-month span. Profiles built before
    ``earliest_class_date`` existed can still participate by using their lone
    latest/earliest date as a one-month span; fully undated or inverted spans
    are invalid and reported by ``build_zip_demand_report``.
    """
    start = start_date or end_date
    end = end_date or start_date
    if not start or not end:
        return None
    try:
        a = datetime.strptime(start[:10], "%Y-%m-%d")
        b = datetime.strptime(end[:10], "%Y-%m-%d")
    except ValueError:
        return None
    months = (b.year - a.year) * 12 + (b.month - a.month) + 1
    return months if months > 0 else None


def _finite_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if math.isfinite(v):
            return v
    return default


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Linear percentile for non-negative finite values."""
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    rank = max(0.0, min(1.0, pct)) * (len(vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return vals[lo]
    frac = rank - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def build_zip_demand_report(
    demand_by_zip: Mapping[str, ZipDemandProfile],
    centroids: Optional[Mapping[str, Tuple[float, float]]] = None,
    reference_avg: Optional[float] = None,
    latest_export_date: Optional[str] = None,
) -> Dict[str, Any]:
    """JSON-safe report-wide ZIP demand dataset.

    One row per demand ZIP carrying the demand score, raw class history (total
    and per charted channel), and centroid status, plus the centroid-coverage
    audit and a ``charts`` block: a shared point set and four least-squares
    fits of an actual demand outcome (ARC CPR / ARC BLS / AHA BLS students,
    and total held class count) against the ZIP demand score. Pure: the
    renderers (table, scatters, map) consume this, never the raw profiles, so
    every view stays consistent.
    """
    centroids = centroids or {}

    def _bucket_students(profile: ZipDemandProfile, key: str) -> int:
        b = profile.buckets.get(key)
        return int(b.students) if b is not None else 0

    def _bucket_avg_students(profile: ZipDemandProfile, key: str) -> float:
        b = profile.buckets.get(key)
        if b is None or b.classes <= 0:
            return 0.0
        if isinstance(b.avg_students, (int, float)):
            return round(float(b.avg_students), 2)
        return round(float(b.students) / b.classes, 2)

    rows: List[Dict[str, Any]] = []
    for z in sorted(demand_by_zip):
        profile = demand_by_zip[z]
        score = compute_zip_demand_score(
            profile, reference_avg=reference_avg,
            latest_export_date=latest_export_date)
        centroid = centroids.get(z)
        month_span = _inclusive_month_span(
            profile.earliest_class_date, profile.latest_class_date)
        total_students = int(profile.total_students or 0)
        classes_per_month = (
            round(float(profile.total_classes) / month_span, 4)
            if month_span else None)
        students_per_month = (
            round(float(total_students) / month_span, 4)
            if month_span else None)
        arc_cpr_students = _bucket_students(profile, ARC_CPR)
        arc_bls_students = _bucket_students(profile, ARC_BLS)
        aha_bls_students = _bucket_students(profile, AHA_BLS)
        arc_cpr_avg_students = _bucket_avg_students(profile, ARC_CPR)
        arc_bls_avg_students = _bucket_avg_students(profile, ARC_BLS)
        aha_bls_avg_students = _bucket_avg_students(profile, AHA_BLS)
        arc_cpr_students_per_month = (
            round(float(arc_cpr_students) / month_span, 4)
            if month_span else None)
        arc_bls_students_per_month = (
            round(float(arc_bls_students) / month_span, 4)
            if month_span else None)
        aha_bls_students_per_month = (
            round(float(aha_bls_students) / month_span, 4)
            if month_span else None)
        rows.append({
            "zip": z,
            "demand_score": score,
            "strength": demand_strength_category(score),
            "classes": profile.total_classes,
            "total_students": total_students,
            "month_span": month_span,
            "held_classes_per_month": classes_per_month,
            "students_per_month": students_per_month,
            # Per-channel student (people) counts — 0 when that channel had no
            # enrolled classes in the ZIP (a real zero, not missing data).
            "arc_cpr_students": arc_cpr_students,
            "arc_bls_students": arc_bls_students,
            "aha_bls_students": aha_bls_students,
            "arc_cpr_avg_students_per_class": arc_cpr_avg_students,
            "arc_bls_avg_students_per_class": arc_bls_avg_students,
            "aha_bls_avg_students_per_class": aha_bls_avg_students,
            "arc_cpr_students_per_month": arc_cpr_students_per_month,
            "arc_bls_students_per_month": arc_bls_students_per_month,
            "aha_bls_students_per_month": aha_bls_students_per_month,
            "avg_students": profile.average_students_per_class,
            "fill_rate": profile.fill_rate,
            "confidence_modifier": compute_confidence_modifier(
                profile, latest_export_date=latest_export_date),
            "centroid_present": centroid is not None,
            "lat": centroid[0] if centroid else None,
            "lng": centroid[1] if centroid else None,
            "earliest_class_date": profile.earliest_class_date,
            "latest_class_date": profile.latest_class_date,
        })
    # Rank by demand score (None last), then ZIP — the table's default order.
    rows.sort(key=lambda r: (-(r["demand_score"] or -1.0), r["zip"]))

    coverage = audit_centroid_coverage(demand_by_zip, centroids)

    # Validation charts: x = ZIP demand score (the model's prediction) for every
    # chart; y = an actual historical demand outcome. A ZIP appears in every
    # chart as long as it has a score — a zero outcome plots at y=0, never
    # dropped. (Only score-less ZIPs are excluded, and total_classes>0 implies a
    # score, so in practice none are.) Each point carries every tooltip field;
    # each series adds its own least-squares fit (R²/slope/n).
    excluded_points: List[Dict[str, Any]] = []
    chart_rows = []
    for r in rows:
        reasons = []
        if not isinstance(r["demand_score"], (int, float)):
            reasons.append("no valid ZIP demand score")
        if not isinstance(r.get("month_span"), int) or r["month_span"] <= 0:
            reasons.append("no valid historical month span")
        if reasons:
            excluded_points.append({"zip": r["zip"], "reasons": reasons})
        else:
            chart_rows.append(r)
    points = [
        {"zip": r["zip"], "demand_score": r["demand_score"],
         "classes": r["classes"],
         "total_students": r["total_students"],
         "month_span": r["month_span"],
         "held_classes_per_month": r["held_classes_per_month"],
         "students_per_month": r["students_per_month"],
         "arc_cpr_students": r["arc_cpr_students"],
         "arc_bls_students": r["arc_bls_students"],
         "aha_bls_students": r["aha_bls_students"],
         "arc_cpr_avg_students_per_class": r["arc_cpr_avg_students_per_class"],
         "arc_bls_avg_students_per_class": r["arc_bls_avg_students_per_class"],
         "aha_bls_avg_students_per_class": r["aha_bls_avg_students_per_class"],
         "arc_cpr_students_per_month": r["arc_cpr_students_per_month"],
         "arc_bls_students_per_month": r["arc_bls_students_per_month"],
         "aha_bls_students_per_month": r["aha_bls_students_per_month"],
         "avg_students": r["avg_students"], "fill_rate": r["fill_rate"],
         "confidence_modifier": r["confidence_modifier"]}
        for r in chart_rows
    ]
    def _series(
        key: str,
        title: str,
        y_label: str,
        *,
        hide_zero_y: bool = False,
    ) -> Dict[str, Any]:
        eligible = [
            p for p in points
            if isinstance(p.get(key), (int, float))
            and (not hide_zero_y or float(p[key]) > 0.0)
        ]
        fit = _ols(
            [float(p["demand_score"]) for p in eligible],
            [float(p[key]) for p in eligible],
        )
        hidden_zero_y = len(points) - len(eligible) if hide_zero_y else 0
        return {
            "key": key, "title": title, "y_label": y_label,
            "n": len(eligible), "enough_data": fit is not None,
            "slope": fit["slope"] if fit else None,
            "intercept": fit["intercept"] if fit else None,
            "r_squared": fit["r_squared"] if fit else None,
            "hide_zero_y": hide_zero_y,
            "hidden_zero_y": hidden_zero_y,
        }

    # Combined historical demand index (the 5th chart's y-axis). It balances
    # month-normalized volume with efficiency, so raw historical class count
    # cannot dominate the opportunity read. Each metric is normalized against a
    # high-percentile cohort benchmark and capped at 100; this keeps one extreme
    # ZIP from stretching everyone else down. Real zeros remain zero.
    _INDEX_METRICS = (
        "students_per_month",
        "held_classes_per_month",
        "avg_students",
        "fill_rate",
        "arc_cpr_students_per_month",
        "arc_bls_students_per_month",
        "aha_bls_students_per_month",
    )

    def _normalizer(key: str):
        vals = [max(0.0, _finite_number(p.get(key))) for p in points]
        positives = [v for v in vals if v > 0]
        hi = _percentile(positives, 0.90) or 0.0
        if hi <= 1e-9:
            return lambda v: 0.0
        return lambda v: min(100.0, 100.0 * max(0.0, _finite_number(v)) / hi)

    _norms = {k: _normalizer(k) for k in _INDEX_METRICS}
    for p in points:
        normalized = {f"normalized_{k}": round(_norms[k](p.get(k)), 2)
                      for k in _INDEX_METRICS}
        p.update(normalized)
        p["normalized_avg_students_per_class"] = p.pop("normalized_avg_students")
        index_components = [
            p["normalized_students_per_month"],
            p["normalized_held_classes_per_month"],
            p["normalized_avg_students_per_class"],
            p["normalized_fill_rate"],
            p["normalized_arc_cpr_students_per_month"],
            p["normalized_arc_bls_students_per_month"],
            p["normalized_aha_bls_students_per_month"],
        ]
        p["combined_historical_demand_index"] = round(
            sum(index_components) / len(index_components), 2)
        # Business-facing name for the final opportunity chart, plus a
        # backward-compatible alias for older consumers.
        p["historical_demand_score"] = p["combined_historical_demand_index"]
        p["combined_demand_index"] = p["combined_historical_demand_index"]

    charts = {
        "x_label": "ZIP demand score",
        "points": points,
        # ZIPs present vs excluded. Surfaced so the report can state why
        # anything is absent rather than dropping silently.
        "zips_plotted": len(points),
        "zips_excluded_no_score": sum(
            1 for e in excluded_points
            if "no valid ZIP demand score" in e["reasons"]),
        "zips_excluded_no_valid_month_span": sum(
            1 for e in excluded_points
            if "no valid historical month span" in e["reasons"]),
        "zips_excluded": excluded_points,
        "series": [
            _series("arc_cpr_avg_students_per_class",
                    "ZIP demand score vs ARC CPR avg students/class",
                    "ARC CPR avg students/class",
                    hide_zero_y=True),
            _series("arc_bls_avg_students_per_class",
                    "ZIP demand score vs ARC BLS avg students/class",
                    "ARC BLS avg students/class",
                    hide_zero_y=True),
            _series("aha_bls_avg_students_per_class",
                    "ZIP demand score vs AHA BLS avg students/class",
                    "AHA BLS avg students/class",
                    hide_zero_y=True),
            _series("avg_students",
                    "ZIP demand score vs overall avg students/class",
                    "Overall avg students/class",
                    hide_zero_y=True),
        ],
        # The final boss-facing opportunity chart, rendered after the four.
        "combined": _series(
            "historical_demand_score",
            "Final ZIP opportunity — ZIP score vs historical demand score",
            "Historical demand score (0–100)"),
    }

    return {
        "rows": rows,
        "total_zips": len(rows),
        "total_classes": sum(r["classes"] for r in rows),
        "coverage": coverage.to_dict(),
        "missing_centroid_zips": list(coverage.missing_zips),
        "charts": charts,
    }


def _haversine_miles(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    # 3958.8 = mean Earth radius in miles; clamp h against float overshoot.
    return 3958.8 * 2 * math.asin(math.sqrt(min(1.0, h)))


def resolve_candidate_zips(
    demand_by_zip: Mapping[str, ZipDemandProfile],
    candidate_zip: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    city: Optional[str] = None,
    centroids: Optional[Mapping[str, Tuple[float, float]]] = None,
    radius_miles: float = DEFAULT_RADIUS_MILES,
) -> Tuple[List[str], str]:
    """Resolve which demand ZIPs describe a candidate.

    Precedence (most precise wins):
      1. exact_plus_radius — candidate ZIP/coords + centroid file: the
         candidate's ZIP (when it has demand) plus demand ZIPs within
         ``radius_miles``; candidate's own ZIP listed first.
      2. exact — candidate ZIP has demand, but no centroids/coords for radius.
      3. city — ZIPs whose held classes were in the candidate's city.
      4. none.
    """
    # --- exact + radius -------------------------------------------------- #
    origin: Optional[Tuple[float, float]] = None
    if latitude is not None and longitude is not None:
        origin = (float(latitude), float(longitude))
    elif candidate_zip and centroids and candidate_zip in centroids:
        origin = centroids[candidate_zip]

    if origin is not None and centroids:
        nearby = [
            z for z in demand_by_zip
            if z in centroids
            and _haversine_miles(origin, centroids[z]) <= radius_miles
        ]
        if candidate_zip and candidate_zip in demand_by_zip \
                and candidate_zip not in nearby:
            nearby.insert(0, candidate_zip)
        if nearby:
            nearby.sort(key=lambda z: (z != candidate_zip, z))
            return nearby, "exact_plus_radius"

    # --- exact ------------------------------------------------------------ #
    if candidate_zip and candidate_zip in demand_by_zip:
        return [candidate_zip], "exact"

    # --- city fallback ----------------------------------------------------- #
    city_n = _norm(city)
    if city_n:
        matched = sorted(
            z for z, p in demand_by_zip.items() if city_n in p.cities
        )
        if matched:
            return matched, "city"

    return [], "none"


# --------------------------------------------------------------------------- #
# Per-candidate payload: profile["zip_demand"]
# --------------------------------------------------------------------------- #

def _merge_profiles(profiles: List[ZipDemandProfile],
                    label: str) -> ZipDemandProfile:
    """Combine several resolved ZIP profiles into one for scoring. Sums are
    exact; fill_rate is re-derived from the summed pairs; latest date is the
    max. Averages recomputed from the sums."""
    merged = ZipDemandProfile(
        zip=label, buckets={b: BucketStat() for b in REPORT_BUCKETS})
    filled, cap = 0, 0
    fill_known = False
    # Per-bucket fill reconstruction, same rate*capacity trick as the ZIP level.
    bucket_fill: Dict[str, Tuple[int, int]] = {}
    for p in profiles:
        merged.total_classes += p.total_classes
        if p.total_students is not None:
            merged.total_students = (merged.total_students or 0) + p.total_students
        if p.total_capacity is not None:
            merged.total_capacity = (merged.total_capacity or 0) + p.total_capacity
        if p.fill_rate is not None and p.total_capacity:
            # Reconstruct paired sums from rate * capacity. cap deliberately
            # uses total_capacity as the significance weight: fill_rate is
            # already a ratio, so this preserves it and weights ZIPs by volume.
            filled += round(p.fill_rate / 100.0 * p.total_capacity)
            cap += p.total_capacity
            fill_known = True
        if p.latest_class_date and (
                merged.latest_class_date is None
                or p.latest_class_date > merged.latest_class_date):
            merged.latest_class_date = p.latest_class_date
        if p.earliest_class_date and (
                merged.earliest_class_date is None
                or p.earliest_class_date < merged.earliest_class_date):
            merged.earliest_class_date = p.earliest_class_date
        for b, stat in p.buckets.items():
            if b in merged.buckets:
                merged.buckets[b].classes += stat.classes
                merged.buckets[b].students += stat.students
                if stat.fill_rate is not None and stat.capacity:
                    bf, bc = bucket_fill.get(b, (0, 0))
                    bucket_fill[b] = (
                        bf + round(stat.fill_rate / 100.0 * stat.capacity),
                        bc + stat.capacity)
        for c in p.cities:
            if c not in merged.cities:
                merged.cities.append(c)
    if merged.total_students is not None and merged.total_classes > 0:
        merged.average_students_per_class = round(
            merged.total_students / merged.total_classes, 2)
    if fill_known and cap > 0:
        merged.fill_rate = round(100.0 * filled / cap, 1)
    for b in merged.buckets.values():
        if b.classes > 0:
            b.avg_students = round(b.students / b.classes, 2)
    for b, (bf, bc) in bucket_fill.items():
        if bc > 0:
            merged.buckets[b].capacity = bc
            merged.buckets[b].fill_rate = round(100.0 * bf / bc, 1)
    return merged


def build_candidate_zip_demand(
    demand_by_zip: Mapping[str, ZipDemandProfile],
    candidate_zip: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    city: Optional[str] = None,
    centroids: Optional[Mapping[str, Tuple[float, float]]] = None,
    radius_miles: float = DEFAULT_RADIUS_MILES,
    reference_avg: Optional[float] = None,
    latest_export_date: Optional[str] = None,
) -> Dict[str, Any]:
    """The JSON-safe ``profile["zip_demand"]`` payload for one candidate.

    No matching ZIP history => a neutral payload (score None, adjustment 0,
    confidence modifier 0) — the section is informational-absent, never faked.
    """
    zips, basis = resolve_candidate_zips(
        demand_by_zip, candidate_zip=candidate_zip,
        latitude=latitude, longitude=longitude, city=city,
        centroids=centroids, radius_miles=radius_miles,
    )
    profiles = [demand_by_zip[z] for z in zips]
    if profiles:
        combined = (profiles[0] if len(profiles) == 1
                    else _merge_profiles(profiles, label="+".join(zips)))
        score = compute_zip_demand_score(
            combined, reference_avg=reference_avg,
            latest_export_date=latest_export_date)
    else:
        combined, score = None, None

    primary = None
    if zips:
        primary = candidate_zip if candidate_zip in zips else zips[0]

    return {
        "resolved_zips": zips,
        "match_basis": basis,
        "primary_zip": primary,
        "zip_demand_score": score,
        "adjustment": compute_score_adjustment(score) if score is not None else 0.0,
        "confidence_modifier": compute_confidence_modifier(
            combined, latest_export_date=latest_export_date),
        "strength": demand_strength_category(score) if score is not None else None,
        "profiles": [p.to_dict() for p in profiles],
        "combined": combined.to_dict() if combined is not None else None,
        "radius_miles": radius_miles,
        "bucket_labels": dict(BUCKET_LABELS),
    }


def overall_reference_avg(records: List[EnrollwareClassRecord]) -> Optional[float]:
    """ALLCPR overall avg students/class over held classes — the reference the
    caller threads into scoring (mirrors historical_performance)."""
    vals = [float(r.enrolled) for r in held_classes(records)
            if r.enrolled and r.enrolled > 0]
    return round(sum(vals) / len(vals), 2) if vals else None


def latest_export_date(records: List[EnrollwareClassRecord]) -> Optional[str]:
    """Latest class date in the whole export — the recency anchor."""
    dates = [r.date for r in records if r.date]
    return max(dates) if dates else None
