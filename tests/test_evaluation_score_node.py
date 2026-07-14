"""Tests for the ScoreNode evidence primitive (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.score_node import ScoreNode  # noqa: E402


def test_present_node_carries_its_values():
    node = ScoreNode(
        key="historical_performance",
        label="Historical enrollment",
        value=4.68,
        weight=0.35,
        confidence=0.82,
        contribution=21.0,
        reasons=["Above the local course average."],
    )
    assert node.key == "historical_performance"
    assert node.value == 4.68
    assert node.missing is False


def test_missing_node_keeps_value_none_and_zero_contribution():
    """A missing signal must not be fabricated into a number."""
    node = ScoreNode(
        key="forecast_expected_students",
        label="Forecast",
        value=None,
        weight=0.10,
        confidence=0.0,
        contribution=0.0,
        reasons=["No forecast available."],
        missing=True,
    )
    assert node.missing is True
    assert node.value is None
    assert node.contribution == 0.0


def test_to_dict_is_json_ready():
    node = ScoreNode(
        key="public_demand",
        label="Public demand",
        value=62.0,
        weight=0.20,
        confidence=0.7,
        contribution=12.0,
        reasons=["Healthcare ecosystem supports demand."],
    )
    d = node.to_dict()
    assert d == {
        "key": "public_demand",
        "label": "Public demand",
        "value": 62.0,
        "weight": 0.20,
        "confidence": 0.7,
        "contribution": 12.0,
        "reasons": ["Healthcare ecosystem supports demand."],
        "missing": False,
    }


def test_reasons_default_to_empty_list_per_instance():
    a = ScoreNode(key="a", label="A", value=1.0, weight=0.1,
                  confidence=0.5, contribution=0.1)
    b = ScoreNode(key="b", label="B", value=2.0, weight=0.1,
                  confidence=0.5, contribution=0.2)
    a.reasons.append("only on a")
    assert b.reasons == []
