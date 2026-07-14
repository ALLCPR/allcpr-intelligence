"""
Phase 5 — Evaluation Graph + Decision Engine.

An honest, deterministic scoring graph that explains *why* a course/location
recommendation is strong or weak. Instead of pretending to predict unknown
future students, it shows a transparent weighted evidence graph built only from
data the upstream collectors and enrichers actually produced.

Public surface:
  - ScoreNode               — one explainable evidence component
  - ConfidencePenalty       — deterministic confidence reduction
  - CourseRecommendation    — EXPAND / MAINTAIN / TEST_ONLY / AVOID
  - ScoreGraphResult        — a course's full opportunity graph
  - build_course_score_graph / build_evaluation_graph — builders
"""
from __future__ import annotations

from app.evaluation.confidence_penalty import (
    ConfidencePenalty,
    compute_confidence_penalty,
)
from app.evaluation.course_recommendation import (
    AVOID,
    EXPAND,
    MAINTAIN,
    TEST_ONLY,
    CourseRecommendation,
    recommend_course,
)
from app.evaluation.evaluation_pipeline import build_evaluation_graph
from app.evaluation.score_graph import ScoreGraphResult, build_course_score_graph
from app.evaluation.score_node import ScoreNode

__all__ = [
    "ScoreNode",
    "ConfidencePenalty",
    "compute_confidence_penalty",
    "CourseRecommendation",
    "recommend_course",
    "EXPAND",
    "MAINTAIN",
    "TEST_ONLY",
    "AVOID",
    "ScoreGraphResult",
    "build_course_score_graph",
    "build_evaluation_graph",
]
