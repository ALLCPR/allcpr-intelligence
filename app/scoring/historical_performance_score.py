"""
Historical performance score (STEP 6) — 0..100.

Answers "what does ALLCPR's own class history say about this area?" by rolling
up real Enrollware class records into a single 0..100 score with an explicit
confidence and human-readable reasons. It is the historical counterpart to the
forward-looking site score: site score asks "is this a good place?", this asks
"have we actually performed well here before?".

Pure and deterministic. Every factor that depends on a field the export did not
contain is simply dropped and the remaining factors are re-weighted — nothing is
imputed. When there is no usable history (no records, or no class carries an
enrollment count) the function returns ``None`` so callers omit the section.

Factors (weights re-normalized over whichever are known):
  - enrollment  (0.45) — average students per class, anchored so a healthy
    ~12-student class approaches 100; parity with a supplied ALLCPR-wide
    reference scores 50.
  - fill rate   (0.30) — seats filled / seats offered, where capacity is known.
  - growth      (0.25) — recent monthly enrollment vs earlier months.
  - sample size — does not add points; it sets the confidence and lightly
    discounts a score built on very few classes.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional

from app.collectors.enrollware import EnrollwareClassRecord

# Factor weights (before re-normalization over the known factors).
_W_ENROLLMENT = 0.45
_W_FILL = 0.30
_W_GROWTH = 0.25

# Enrollment anchor: an area averaging this many students/class scores 100 on
# the absolute scale (used only when no ALLCPR-wide reference is supplied).
_ENROLL_TARGET = 12.0

# Sample-size thresholds for confidence + the small-sample discount.
_MIN_CLASSES = 3      # below this we will not score at all
_LOW_SAMPLE = 8       # below this the score is gently discounted
_HIGH_SAMPLE = 20     # at/above this, sample is no longer a confidence limiter


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _enrollment_score(
    avg_students: float, reference_avg: Optional[float]
) -> float:
    """50 at parity with the ALLCPR-wide reference; absolute anchor otherwise."""
    if reference_avg and reference_avg > 0:
        return _clamp(50.0 * (avg_students / reference_avg))
    return _clamp(100.0 * (avg_students / _ENROLL_TARGET))


def _fill_score(records: List[EnrollwareClassRecord]) -> Optional[float]:
    pairs = [
        (r.enrolled, r.capacity) for r in records
        if r.enrolled is not None and r.capacity is not None and r.capacity > 0
    ]
    if not pairs:
        return None
    seats_filled = sum(e for e, _ in pairs)
    seats_total = sum(c for _, c in pairs)
    if seats_total <= 0:
        return None
    return _clamp(100.0 * seats_filled / seats_total)


def _growth(records: List[EnrollwareClassRecord]) -> Optional[Dict[str, Any]]:
    """Compare later-half vs earlier-half monthly average enrollment.

    Returns ``None`` when fewer than two distinct months carry enrollment.
    """
    by_month: Dict[str, List[int]] = {}
    for r in records:
        if r.month and r.enrolled is not None:
            by_month.setdefault(r.month, []).append(r.enrolled)
    months = sorted(by_month)
    if len(months) < 2:
        return None
    monthly_avg = [mean(by_month[m]) for m in months]
    half = len(monthly_avg) // 2
    earlier = mean(monthly_avg[:half]) if half else monthly_avg[0]
    later = mean(monthly_avg[half:])
    if earlier <= 0:
        return None
    ratio = later / earlier
    # ratio 1.0 -> 50 (flat); 2.0 -> 100 (doubled); 0.5 -> 25.
    score = _clamp(50.0 + 50.0 * (ratio - 1.0))
    return {"score": score, "ratio": round(ratio, 2),
            "earlier_avg": round(earlier, 2), "later_avg": round(later, 2),
            "months": len(months)}


def _confidence(n_classes: int, known_factors: int) -> str:
    if n_classes >= _HIGH_SAMPLE and known_factors >= 2:
        return "high"
    if n_classes >= _LOW_SAMPLE:
        return "medium"
    return "low"


def score_historical_performance(
    records: List[EnrollwareClassRecord],
    reference_avg: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Score ALLCPR's historical performance for one area's class records.

    ``records`` should already be filtered to the area of interest.
    ``reference_avg`` is the optional ALLCPR-wide average students/class, used
    to anchor the enrollment factor at parity = 50.

    Returns ``{score, confidence, reasons, components, sample_size}`` or
    ``None`` when there is no usable history.
    """
    if not records:
        return None

    # Only classes that actually ran (enrolled > 0) inform performance; a
    # cancelled/zero-enrollment row is not evidence of demand.
    enrolled_vals = [r.enrolled for r in records if r.enrolled and r.enrolled > 0]
    if len(enrolled_vals) < _MIN_CLASSES:
        return None  # not enough real history to score

    avg_students = mean(enrolled_vals)
    components: Dict[str, Any] = {}
    reasons: List[str] = []

    # Enrollment (always present once we get here).
    enroll_score = _enrollment_score(avg_students, reference_avg)
    components["enrollment"] = {
        "score": round(enroll_score, 1),
        "avg_students_per_class": round(avg_students, 2),
        "reference_avg": reference_avg,
    }
    if reference_avg:
        rel = "above" if avg_students >= reference_avg else "below"
        reasons.append(
            f"Averages {avg_students:.1f} students/class — {rel} the ALLCPR-wide "
            f"average of {reference_avg:.1f}."
        )
    else:
        reasons.append(f"Averages {avg_students:.1f} students/class historically.")

    weighted_sum = _W_ENROLLMENT * enroll_score
    weight_total = _W_ENROLLMENT

    # Fill rate (optional).
    fill = _fill_score(records)
    if fill is not None:
        components["fill_rate"] = {"score": round(fill, 1)}
        weighted_sum += _W_FILL * fill
        weight_total += _W_FILL
        reasons.append(f"Seats fill at {fill:.0f}% of offered capacity.")

    # Growth (optional).
    growth = _growth(records)
    if growth is not None:
        components["growth"] = growth
        weighted_sum += _W_GROWTH * growth["score"]
        weight_total += _W_GROWTH
        if growth["ratio"] >= 1.1:
            reasons.append(
                f"Enrollment is trending up ({growth['earlier_avg']:.1f} → "
                f"{growth['later_avg']:.1f} students/class)."
            )
        elif growth["ratio"] <= 0.9:
            reasons.append(
                f"Enrollment is trending down ({growth['earlier_avg']:.1f} → "
                f"{growth['later_avg']:.1f} students/class)."
            )
        else:
            reasons.append("Enrollment is roughly flat over time.")

    base_score = weighted_sum / weight_total if weight_total else 0.0

    # Small-sample discount: scores built on very little history are pulled
    # toward the neutral 50 so a 3-class fluke can't read as a slam dunk.
    n = len(enrolled_vals)
    if n < _LOW_SAMPLE:
        shrink = n / _LOW_SAMPLE          # 0..1
        score = 50.0 + (base_score - 50.0) * shrink
        reasons.append(
            f"Based on only {n} class(es) of history — score pulled toward "
            f"neutral until more data accrues."
        )
    else:
        score = base_score

    known_factors = len(components)
    confidence = _confidence(n, known_factors)

    return {
        "score": round(_clamp(score), 1),
        "confidence": confidence,
        "reasons": reasons,
        "components": components,
        "sample_size": n,
    }
