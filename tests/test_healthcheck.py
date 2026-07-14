"""Health-check tests — status classification + report formatting.

The live network checks themselves aren't unit-tested (they're integration
probes); these cover the result model, formatting, and the any_down gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.utils.healthcheck import (  # noqa: E402
    CheckResult,
    _safe,
    any_down,
    format_report,
)


def test_check_result_symbols():
    assert CheckResult("x", "ok", "").symbol == "✓"
    assert CheckResult("x", "down", "").symbol == "✗"
    assert CheckResult("x", "skipped", "").symbol == "–"


def test_safe_catches_exceptions():
    def boom():
        raise RuntimeError("kaboom")
    result = _safe("Exploder", boom)
    assert result.status == "down"
    assert "kaboom" in result.detail
    assert result.name == "Exploder"


def test_safe_passes_through_status():
    result = _safe("Fine", lambda: ("ok", "all good"))
    assert result.status == "ok"
    assert result.detail == "all good"


def test_any_down_true_when_one_down():
    results = [
        CheckResult("a", "ok", ""),
        CheckResult("b", "down", ""),
        CheckResult("c", "skipped", ""),
    ]
    assert any_down(results) is True


def test_any_down_false_when_only_ok_and_skipped():
    results = [
        CheckResult("a", "ok", ""),
        CheckResult("c", "skipped", ""),
    ]
    assert any_down(results) is False


def test_format_report_contains_counts_and_names():
    results = [
        CheckResult("Google Maps", "ok", "geocode OK"),
        CheckResult("Adzuna", "down", "quota"),
        CheckResult("Mapbox", "skipped", "no token"),
    ]
    text = format_report(results)
    assert "Google Maps" in text
    assert "Adzuna" in text
    assert "1 ok · 1 down · 1 skipped" in text
