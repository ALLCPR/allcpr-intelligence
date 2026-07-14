"""
Back-test analysis.

The scoring model's weights and caps are educated guesses — the system has
never been graded against a real outcome. This module is the grader: given
each candidate's scored signals plus a known outcome (enrollment, revenue,
survived/closed, anything monotonic), it reports how well ``site_score`` and
each individual sub-score actually *predict* that outcome.

Pure math, no network — so it's unit-testable and the harness in
``scripts/backtest.py`` only has to handle I/O + running the pipeline.

Two correlation measures:
- Pearson: linear correlation (sensitive to outliers, assumes linearity).
- Spearman: rank correlation (robust, catches any monotonic relationship).
  More trustworthy for the small N a real ALLCPR back-test will have.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient, or None if undefined (n<2 or no spread)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank(values: List[float]) -> List[float]:
    """Average-rank transform (ties share the mean of their rank span)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman rank correlation — Pearson on the rank-transformed values."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return pearson(_rank(xs), _rank(ys))


@dataclass
class SignalCorrelation:
    signal: str
    pearson: Optional[float]
    spearman: Optional[float]
    n: int

    @property
    def strength(self) -> str:
        r = self.spearman if self.spearman is not None else self.pearson
        if r is None:
            return "undefined"
        a = abs(r)
        if a >= 0.7:
            return "strong"
        if a >= 0.4:
            return "moderate"
        if a >= 0.2:
            return "weak"
        return "negligible"

    @property
    def direction(self) -> str:
        r = self.spearman if self.spearman is not None else self.pearson
        if r is None:
            return ""
        return "positive" if r >= 0 else "negative"


@dataclass
class BacktestReport:
    n: int
    outcome_name: str
    correlations: List[SignalCorrelation] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def best_predictor(self) -> Optional[SignalCorrelation]:
        ranked = [c for c in self.correlations
                  if c.spearman is not None or c.pearson is not None]
        if not ranked:
            return None
        return max(
            ranked,
            key=lambda c: abs(c.spearman if c.spearman is not None
                              else c.pearson),
        )


# Sub-scores we always try to correlate (when present in the scored output).
_TRACKED_SIGNALS = (
    "site_score",
    "demand_score",
    "healthcare_training_ecosystem_score",
    "competition_gap_score",
    "allcpr_opportunity_score",
    "economy_score",
    "accessibility_score",
    "profitability_score",
    "confidence_score",
    "job_certification_demand_score",
)


def analyze_backtest(
    rows: List[Dict[str, object]],
    outcome_name: str = "outcome",
) -> BacktestReport:
    """Correlate each tracked signal against the outcome.

    ``rows`` is a list of dicts each containing:
      - ``"outcome"``: the known result (float)
      - ``"site_score"``: float
      - ``"sub_scores"``: dict of sub-score name → float

    Returns a BacktestReport with per-signal Pearson + Spearman, sorted by
    absolute Spearman (most predictive first).
    """
    usable = [r for r in rows if isinstance(r.get("outcome"), (int, float))]
    report = BacktestReport(n=len(usable), outcome_name=outcome_name)
    if len(usable) < 3:
        report.notes.append(
            f"Only {len(usable)} usable rows — correlations need at least "
            f"3 (ideally 8+) to be meaningful. Treat results as directional."
        )
    outcomes = [float(r["outcome"]) for r in usable]

    def values_for(signal: str) -> List[float]:
        out: List[float] = []
        for r in usable:
            if signal == "site_score":
                v = r.get("site_score")
            else:
                v = (r.get("sub_scores") or {}).get(signal)
            out.append(float(v) if isinstance(v, (int, float)) else math.nan)
        return out

    for signal in _TRACKED_SIGNALS:
        vals = values_for(signal)
        # Drop rows where this signal is NaN, pairing with outcomes.
        paired = [(v, o) for v, o in zip(vals, outcomes) if not math.isnan(v)]
        if len(paired) < 2:
            continue
        xs = [p[0] for p in paired]
        ys = [p[1] for p in paired]
        report.correlations.append(SignalCorrelation(
            signal=signal,
            pearson=pearson(xs, ys),
            spearman=spearman(xs, ys),
            n=len(paired),
        ))

    report.correlations.sort(
        key=lambda c: abs(c.spearman if c.spearman is not None
                          else (c.pearson if c.pearson is not None else 0.0)),
        reverse=True,
    )
    return report


def format_report(report: BacktestReport) -> str:
    lines = [
        f"Back-test: {report.n} site(s) vs '{report.outcome_name}'",
        "=" * 64,
    ]
    for note in report.notes:
        lines.append(f"  ! {note}")
    if report.notes:
        lines.append("-" * 64)
    lines.append(f"  {'Signal':<38} {'Spearman':>9} {'Pearson':>9}  Read")
    lines.append("-" * 64)
    for c in report.correlations:
        sp = f"{c.spearman:+.2f}" if c.spearman is not None else "  n/a"
        pe = f"{c.pearson:+.2f}" if c.pearson is not None else "  n/a"
        read = f"{c.strength} {c.direction}".strip()
        lines.append(f"  {c.signal:<38} {sp:>9} {pe:>9}  {read}")
    lines.append("=" * 64)
    best = report.best_predictor()
    if best is not None:
        lines.append(
            f"  Best predictor of {report.outcome_name}: {best.signal} "
            f"({best.strength} {best.direction})"
        )
        lines.append(
            "  → Consider up-weighting the strong positive predictors and "
            "down-weighting signals that show negligible or negative "
            "correlation with real outcomes."
        )
    return "\n".join(lines)
