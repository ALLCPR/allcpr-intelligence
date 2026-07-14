"""
Explanation engine (Phase 5).

Deterministic, plain-English explanations of a course opportunity graph. No LLM,
no randomness — same graph in, same prose out. Every number quoted comes from a
real :class:`ScoreNode` value; missing signals are described as unknown rather
than invented.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.evaluation.score_graph import ScoreGraphResult
from app.evaluation.score_node import ScoreNode


def _node(result: ScoreGraphResult, key: str) -> Optional[ScoreNode]:
    for n in result.nodes:
        if n.key == key:
            return n
    return None


def _confidence_phrase(result: ScoreGraphResult) -> str:
    level = result.penalty.confidence_level.replace("_", "-")
    return f"{level}-confidence"


def explain_graph_node(node: ScoreNode) -> str:
    """One sentence describing a single node."""
    if node.missing or node.value is None:
        return f"{node.label}: unknown — left out of the score, not assumed."
    return (
        f"{node.label}: {node.value} "
        f"(+{node.contribution:.0f} toward the score)."
    )


def explain_recommendation(result: ScoreGraphResult) -> str:
    """One sentence stating the recommendation and why, at a glance."""
    rec = result.recommendation
    return (
        f"{result.label}: {rec.display_group} "
        f"(score {result.final_score:.0f}, {rec.action.replace('_', ' ').lower()}, "
        f"{_confidence_phrase(result)})."
    )


def explain_course_result(result: ScoreGraphResult) -> str:
    """A short paragraph explaining one course's recommendation."""
    rec = result.recommendation
    strong = rec.display_group == "Primary"
    parts: List[str] = []

    lead = (
        f"{result.label} appears strongest in this area."
        if strong else
        f"{result.label} is a weaker option in this area."
    )
    parts.append(lead)

    hist = _node(result, "historical_performance")
    rel = _node(result, "course_relative_performance")
    if hist and not hist.missing and hist.value is not None:
        direction = None
        if rel and not rel.missing and isinstance(rel.value, (int, float)):
            direction = "above" if rel.value >= 0 else "below"
        if direction:
            parts.append(
                f"Historical enrollment is {hist.value} students/class, which is "
                f"{direction} the local course average."
            )
        else:
            parts.append(
                f"Historical enrollment is {hist.value} students/class."
            )
    else:
        parts.append("There is no matching ALLCPR enrollment history here.")

    # Sample size / confidence.
    parts.append(
        f"The evidence supports a {_confidence_phrase(result)} recommendation."
    )

    demand = _node(result, "public_demand")
    if demand and not demand.missing and demand.value is not None:
        if demand.value >= 50:
            parts.append("Public demand and healthcare signals support it.")
        else:
            parts.append("Public demand here is on the soft side.")

    if not strong:
        parts.append(
            "Do not prioritize this course without paid-search validation."
        )

    parts.append(f"Final recommendation: {rec.display_group.upper()}.")
    return " ".join(parts)


def summarize_primary_secondary_avoid(results: List[ScoreGraphResult]) -> str:
    """A compact summary grouping every course by recommendation."""
    buckets: Dict[str, List[str]] = {
        "Primary": [], "Secondary": [], "Avoid / test only": [],
    }
    for r in results:
        buckets.setdefault(r.recommendation.display_group, []).append(
            f"{r.label} ({r.final_score:.0f})"
        )

    def _line(group: str) -> str:
        names = buckets.get(group) or []
        return f"{group}: " + (", ".join(names) if names else "none")

    return "  |  ".join(
        _line(g) for g in ("Primary", "Secondary", "Avoid / test only")
    )
