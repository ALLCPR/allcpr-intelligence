"""
Course recommendation (Phase 5).

Translates a final course opportunity score (0..100) into a deterministic,
report-ready recommendation. Four actions, mapped onto the three report display
groups the existing "Best Course Strategy" section already uses (Primary /
Secondary / Avoid or test only) so the new graph can power it directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Action constants.
EXPAND = "EXPAND"
MAINTAIN = "MAINTAIN"
TEST_ONLY = "TEST_ONLY"
AVOID = "AVOID"

# Score thresholds (inclusive lower bound).
_EXPAND_MIN = 70.0
_MAINTAIN_MIN = 50.0
_TEST_ONLY_MIN = 30.0

# action -> (display_group, short label)
_DISPLAY = {
    EXPAND: ("Primary", "Expand"),
    MAINTAIN: ("Secondary", "Maintain"),
    TEST_ONLY: ("Avoid / test only", "Test only"),
    AVOID: ("Avoid / test only", "Avoid"),
}


@dataclass
class CourseRecommendation:
    action: str
    display_group: str
    label: str
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "display_group": self.display_group,
            "label": self.label,
            "reasons": list(self.reasons),
        }


def _action_for(score: float) -> str:
    if score >= _EXPAND_MIN:
        return EXPAND
    if score >= _MAINTAIN_MIN:
        return MAINTAIN
    if score >= _TEST_ONLY_MIN:
        return TEST_ONLY
    return AVOID


def recommend_course(
    score: float,
    reasons: Optional[List[str]] = None,
    course_label: Optional[str] = None,
) -> CourseRecommendation:
    """Turn a 0..100 course opportunity score into a recommendation.

    ``reasons`` (if given) are carried through verbatim; ``course_label`` is
    only used to make the default reason read naturally.
    """
    action = _action_for(score)
    display_group, label = _DISPLAY[action]
    name = course_label or "This course"
    default_reason = {
        EXPAND: f"{name} scores {score:.0f} — strong enough to treat as a "
                "primary course for this area.",
        MAINTAIN: f"{name} scores {score:.0f} — solid; keep it as a secondary "
                  "course here.",
        TEST_ONLY: f"{name} scores {score:.0f} — weak; only run it as a "
                   "paid-search test, do not prioritize.",
        AVOID: f"{name} scores {score:.0f} — too weak to prioritize in this "
               "area without new evidence.",
    }[action]
    out_reasons = list(reasons) if reasons else [default_reason]
    return CourseRecommendation(
        action=action,
        display_group=display_group,
        label=label,
        reasons=out_reasons,
    )
