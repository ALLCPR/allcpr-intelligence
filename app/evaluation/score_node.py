"""
ScoreNode — one piece of explainable evidence in the course opportunity graph
(Phase 5).

Each node captures a single component of the final course opportunity score:
its raw ``value`` (or ``None`` when unknown — never fabricated), the ``weight``
it carries, the ``confidence`` we have in it (0..1), and the deterministic
``contribution`` it makes to the final score. ``reasons`` explains the node in
plain English; ``missing`` flags a signal we simply did not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScoreNode:
    key: str
    label: str
    value: Optional[float]
    weight: float
    confidence: float
    contribution: float
    reasons: List[str] = field(default_factory=list)
    missing: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable view of this node."""
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "weight": self.weight,
            "confidence": self.confidence,
            "contribution": self.contribution,
            "reasons": list(self.reasons),
            "missing": self.missing,
        }
