"""
JSON output for the scored candidates.

Suitable for downstream dashboards / web UIs. Strips internal-only fields
(raw PlaceProfile objects); keeps everything else, including anchor +
demand_top_places + competitors with full place metadata.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.reports.interpretation import (
    build_candidate_interpretation,
    build_report_interpretation,
)
from app.utils.logging_utils import get_logger
from app.utils.report_safety import strip_sensitive_query_params

logger = get_logger(__name__)

# Keys that hold non-serializable PlaceProfile/objects and are duplicated
# by serializable counterparts; drop before writing.
INTERNAL_KEYS = ("anchor_obj", "demand_top_places_obj", "competitors_obj")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        return strip_sensitive_query_params(value)
    return value


def _clean(profile: Dict[str, Any]) -> Dict[str, Any]:
    return _sanitize({k: v for k, v in profile.items() if k not in INTERNAL_KEYS})


def render_json(ranked: List[Tuple[Dict, Dict]], context: Dict[str, Any]
                ) -> Dict[str, Any]:
    """Render the structured JSON payload.

    The JSON always carries the full detailed data. The deterministic
    interpretation layer is added alongside it (not instead of it) so
    dashboards can reuse the executive verdict without recomputing it.
    """
    out_candidates: List[Dict[str, Any]] = []
    for rank, (profile, scored) in enumerate(ranked, start=1):
        out_candidates.append({
            "rank": rank,
            "profile": _clean(profile),
            "scored": _sanitize(scored),
            "interpretation": _sanitize(
                build_candidate_interpretation(profile, scored)
            ),
        })
    payload: Dict[str, Any] = {
        "context": _sanitize(context),
        "report_interpretation": _sanitize(build_report_interpretation(ranked)),
        "candidates": out_candidates,
    }
    # Phase 5 — surface the course opportunity graph at the top level so the
    # report stays machine-readable without digging into course_performance.
    perf = (context or {}).get("course_performance") or {}
    evaluation_graph = perf.get("evaluation_graph")
    if evaluation_graph:
        payload["evaluation_graph"] = _sanitize(evaluation_graph)

    # Score vs Actual Enrollment Validation — surfaced top-level alongside the
    # graph. Use the attached payload when present; otherwise compute it so the
    # JSON always carries the validation when course history exists.
    regression_validation = perf.get("regression_validation")
    if regression_validation is None and perf:
        from app.scoring.regression_validation import build_regression_validation
        regression_validation = build_regression_validation(perf)
    if regression_validation:
        payload["regression_validation"] = _sanitize(regression_validation)

    course_benchmarks = perf.get("course_enrollment_benchmarks")
    if course_benchmarks:
        payload["course_enrollment_benchmarks"] = _sanitize(course_benchmarks)

    # Business-facing center-opening recommendation layer: one plain decision
    # per candidate area, with site-readiness downgrades. This is a summary
    # over existing scores, not a scoring change.
    if out_candidates:
        from app.evaluation.center_recommendations import (
            build_center_recommendations_from_report,
        )
        payload["center_opening_recommendations"] = _sanitize(
            build_center_recommendations_from_report(payload)
        )
    return payload


def write_json_report(ranked: List[Tuple[Dict, Dict]],
                      context: Dict[str, Any],
                      path: Path) -> None:
    payload = render_json(ranked, context)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, default=str, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Saved JSON report ({len(ranked)} candidates) -> {path}")
