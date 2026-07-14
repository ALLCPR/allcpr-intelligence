"""Report integration tests for the Course Opportunity Graph (Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluation.evaluation_pipeline import build_evaluation_graph  # noqa: E402
from app.reports.html_report import _course_performance_section  # noqa: E402
from app.reports.json_report import render_json  # noqa: E402
from app.reports.markdown_report import _evaluation_graph_markdown  # noqa: E402


def _perf_with_graph():
    perf = {
        "area_label": "Milpitas, CA",
        "area_is_filtered": True,
        "total_classes": 46,
        "overall": {"average_students_per_class": 5.0, "total_classes": 46},
        "data_coverage": {"price": False},
        "course_types": [
            {"course_type": "arc_cpr", "label": "ARC CPR",
             "average_students_per_class": 9.0, "total_classes": 40,
             "fill_rate_percent": 80, "course_performance_score": 85,
             "performance_band": "A", "classes_held": 38, "total_students": 360},
            {"course_type": "skills_session", "label": "Skills Session",
             "average_students_per_class": 2.2, "total_classes": 6,
             "fill_rate_percent": None, "course_performance_score": 25,
             "performance_band": "D", "classes_held": 5, "total_students": 12},
        ],
        "schedule_intelligence": {
            "best_day": {"label": "Saturday", "basis": "enrollment",
                         "average_students_per_class": 8.0, "classes": 10},
        },
    }
    perf["evaluation_graph"] = build_evaluation_graph(
        perf,
        demand={"demand_score": 70, "healthcare_training_ecosystem_score": 65},
        competition={"competition_gap_score": 60},
    )
    return perf


def test_html_renders_course_opportunity_graph():
    html = _course_performance_section({"course_performance": _perf_with_graph()})
    assert "Course Opportunity Graph" in html
    assert "ARC CPR" in html
    # A horizontal contribution bar is rendered (CSS width %, no JS libs).
    assert "width:" in html
    # Recommendation surfaced.
    assert "Primary" in html


def test_html_renders_stacked_contribution_chart():
    """Each course gets a visual stacked bar of per-component contributions."""
    html = _course_performance_section({"course_performance": _perf_with_graph()})
    # The stacked-bar container and a colour legend are present.
    assert "eval-stack" in html
    assert "eval-legend" in html
    # Each component is named in the legend so colours are decodable.
    assert "Historical" in html and "Public demand" in html


def test_html_no_graph_falls_back_gracefully():
    perf = _perf_with_graph()
    perf.pop("evaluation_graph")
    html = _course_performance_section({"course_performance": perf})
    # Still renders the existing course-performance content, no crash.
    assert "ARC CPR" in html
    assert "Course Opportunity Graph" not in html


def test_markdown_renders_graph_table_with_real_values():
    # The block helper returns a list of lines (the module convention); join to
    # inspect the rendered Markdown.
    md = "\n".join(_evaluation_graph_markdown(_perf_with_graph()["evaluation_graph"]))
    assert "Course Opportunity Graph" in md
    assert "ARC CPR" in md
    assert "Skills Session" in md


def test_json_contains_top_level_evaluation_graph():
    context = {"course_performance": _perf_with_graph()}
    payload = render_json([], context)
    assert "evaluation_graph" in payload
    eg = payload["evaluation_graph"]
    assert "course_opportunity_graph" in eg
    assert "ARC CPR" in eg["primary"]
