"""Center-opening decision summary.

Turns the existing course opportunity graph (score + confidence + evidence
nodes) into one plain business decision per course:

    Open / Prioritize · Test first · Keep watching · Avoid for now

This is a thin mapping layer — no new scoring. Scores come from
``score_graph``, confidence from ``confidence_penalty``, reasons from the
graph's own evidence nodes. Nothing is fabricated: courses without a score are
skipped, and low confidence always downgrades the decision.

Honesty: this ranks opportunity from known history and current signals. It is
NOT a guaranteed prediction of future enrollment.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

OPEN = "Open / Prioritize"
TEST = "Test first"
WATCH = "Keep watching"
AVOID = "Avoid for now"
DECISIONS = (OPEN, TEST, WATCH, AVOID)

HONESTY_NOTE = (
    "Opportunity-ranking tool based on known history and current signals — "
    "not a guaranteed prediction of future enrollment. Future results depend "
    "on ads, pricing, schedule timing, student behavior, and competition."
)

# Always-present risk: the future is not in the data.
_FUTURE_RISK = (
    "Future enrollment still depends on ads, pricing, timing, and competition."
)


def decide(score: float, confidence_level: str) -> str:
    """Map (opportunity score, confidence level) to a business decision.

    Bands: >=80 Open · 65–79 Test · 50–64 Watch · <50 Avoid. A "low"
    confidence downgrades one level; "very_low" downgrades two. High score
    with low confidence therefore lands on "Test first", never "Open".
    """
    if score >= 80:
        idx = 0
    elif score >= 65:
        idx = 1
    elif score >= 50:
        idx = 2
    else:
        idx = 3
    level = str(confidence_level or "").lower()
    if level == "low":
        idx += 1
    elif level == "very_low":
        idx += 2
    return DECISIONS[min(idx, len(DECISIONS) - 1)]


def _reasons_for(course: Dict[str, Any], max_reasons: int = 3) -> List[str]:
    """Top evidence reasons, strongest contribution first. Never invented."""
    out: List[str] = []
    nodes = [n for n in (course.get("nodes") or []) if not n.get("missing")]
    nodes.sort(key=lambda n: n.get("contribution") or 0, reverse=True)
    for n in nodes:
        node_reasons = n.get("reasons") or []
        out.append(node_reasons[0] if node_reasons
                   else f"{n.get('label')}: +{(n.get('contribution') or 0):.0f} points.")
        if len(out) >= max_reasons:
            break
    out.extend(r for r in (course.get("reasons") or [])
               if r not in out)
    return out[:max_reasons]


def _risks_for(course: Dict[str, Any], max_risks: int = 4) -> List[str]:
    """Penalty reasons (real, from the confidence engine) + the future caveat."""
    risks = list((course.get("penalty") or {}).get("reasons") or [])[: max_risks - 1]
    risks.append(_FUTURE_RISK)
    return risks


def _next_action_for(decision: str, label: str,
                     best_day: Optional[str]) -> str:
    if decision == OPEN:
        if best_day:
            return f"Open with {best_day} {label} classes first."
        return f"Schedule the first {label} cohort and watch fill rate."
    if decision == TEST:
        return f"Run 1–2 pilot {label} classes before committing."
    if decision == WATCH:
        return f"Hold {label}; re-evaluate after the next batch of enrollment data."
    return f"Skip {label} for now; revisit if demand or history improves."


def build_center_opening_recommendations(
    perf: Optional[Dict[str, Any]],
    location: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the center-opening decision payload from a course-performance dict.

    ``perf`` is the ``course_performance`` payload (with its attached
    ``evaluation_graph``). ``location`` optionally names the candidate
    location/area these courses would run at — it is carried through, never
    guessed.
    """
    perf = perf or {}
    graph = perf.get("evaluation_graph") or {}
    courses = graph.get("course_opportunity_graph") or []
    city = perf.get("area_label")
    best_day = ((perf.get("schedule_intelligence") or {})
                .get("best_day") or {}).get("label")

    recs: List[Dict[str, Any]] = []
    for c in courses:
        score = c.get("final_score")
        if not isinstance(score, (int, float)):
            continue  # no score → no decision, never invented
        conf_level = (c.get("penalty") or {}).get("confidence_level") or "low"
        decision = decide(float(score), conf_level)
        label = c.get("label") or c.get("course_type") or "course"
        recs.append({
            "city": city,
            "location": location,
            "course_type": c.get("course_type"),
            "label": label,
            "opportunity_score": round(float(score), 1),
            "confidence": conf_level,
            "decision": decision,
            "reasons": _reasons_for(c),
            "risks": _risks_for(c),
            "next_action": _next_action_for(decision, label, best_day),
        })

    warning = None
    if not recs:
        warning = ("No scored course history available — no center-opening "
                   "decision can be made.")
    return {
        "honesty_note": HONESTY_NOTE,
        "n": len(recs),
        "warning": warning,
        "recommendations": recs,
    }


# --- output writers ---------------------------------------------------------

def write_recommendations_json(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str),
                    encoding="utf-8")


_CSV_COLUMNS = ("city", "location", "course_type", "label",
                "opportunity_score", "confidence", "decision",
                "reasons", "risks", "next_action")


def write_recommendations_csv(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for rec in payload.get("recommendations") or []:
            row = {k: rec.get(k) for k in _CSV_COLUMNS}
            row["reasons"] = " | ".join(rec.get("reasons") or [])
            row["risks"] = " | ".join(rec.get("risks") or [])
            writer.writerow(row)


def format_terminal_summary(payload: Dict[str, Any]) -> str:
    """Human-readable summary matching the report's honest tone."""
    lines: List[str] = []
    for rec in payload.get("recommendations") or []:
        where = " — ".join(x for x in (rec.get("city"), rec.get("location"))
                           if x)
        lines.append(f"{where + ' — ' if where else ''}{rec.get('label')}")
        lines.append(f"  Score: {rec.get('opportunity_score'):.0f}")
        lines.append(f"  Confidence: {str(rec.get('confidence')).replace('_', '-').title()}")
        lines.append(f"  Decision: {rec.get('decision')}")
        lines.append("  Reasons:")
        lines.extend(f"  - {r}" for r in rec.get("reasons") or [])
        lines.append("  Risks:")
        lines.extend(f"  - {r}" for r in rec.get("risks") or [])
        lines.append(f"  Next action: {rec.get('next_action')}")
        lines.append("")
    if payload.get("warning"):
        lines.append(f"! {payload['warning']}")
    lines.append(f"Note: {payload.get('honesty_note')}")
    return "\n".join(lines)
