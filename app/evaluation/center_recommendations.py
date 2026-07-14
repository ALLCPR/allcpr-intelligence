"""Business-facing center opening recommendations.

This layer combines the existing candidate area score, site-validation status,
data-confidence score, and course opportunity graph into one plain decision:

    Open / Prioritize · Test first · Keep watching · Avoid for now

It does not change scoring math. It only downgrades risky recommendations when
the site is not lease-ready or the expansion readiness is weak.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

OPEN = "Open / Prioritize"
TEST = "Test first"
WATCH = "Keep watching"
AVOID = "Avoid for now"
DECISION_LABELS = (OPEN, TEST, WATCH, AVOID)

WARNING_NOTE = (
    "This is an opportunity-ranking recommendation, not a guaranteed enrollment "
    "prediction. Future enrollment still depends on ads, pricing, schedule "
    "timing, Red Cross visibility, instructor availability, and student behavior."
)

CSV_COLUMNS = (
    "city",
    "location_name",
    "address",
    "course_type",
    "area_score",
    "opportunity_score",
    "confidence_score",
    "data_confidence_label",
    "expansion_readiness",
    "site_validation_status",
    "decision_label",
    "decision_reason",
    "main_reasons",
    "main_risks",
    "suggested_next_action",
    "evidence_summary",
    "warning_note",
)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt_score(value: Any) -> Optional[float]:
    n = _num(value)
    return round(n, 1) if n is not None else None


def data_confidence_label(confidence_score: Optional[float]) -> str:
    if confidence_score is None:
        return "Unknown data confidence"
    if confidence_score >= 85:
        return "Very high data confidence"
    if confidence_score >= 70:
        return "High data confidence"
    if confidence_score >= 50:
        return "Medium data confidence"
    return "Low data confidence"


def _site_validated(scored: Dict[str, Any]) -> bool:
    flags = scored.get("validation_flags") or {}
    return (
        scored.get("site_score_status") == "validated"
        or bool(flags.get("lease_ready"))
        or bool(flags.get("commercial_listing_validated"))
    )


def site_validation_status(scored: Dict[str, Any], profile: Dict[str, Any]) -> str:
    if _site_validated(scored):
        return "Validated commercial site"
    name = str(profile.get("candidate_name") or "").lower()
    if "needs commercial site validation" in name:
        return "Not validated — needs commercial site validation"
    return "Not validated — not a confirmed leasing opportunity"


def expansion_readiness(scored: Dict[str, Any], area_score: Optional[float]) -> str:
    if not _site_validated(scored):
        return "Weak"
    if area_score is not None and area_score >= 80:
        return "Strong"
    return "Moderate"


def _downgrade(decision: str) -> str:
    order = [OPEN, TEST, WATCH, AVOID]
    try:
        return order[min(order.index(decision) + 1, len(order) - 1)]
    except ValueError:
        return WATCH


def decide_center_opening(
    area_score: Optional[float],
    *,
    opportunity_score: Optional[float],
    confidence_score: Optional[float],
    readiness: str,
    site_validated: bool,
    demand_score: Optional[float] = None,
) -> str:
    """Map current signals to one business decision.

    This intentionally treats site readiness as a gate. High data confidence is
    not opening confidence, and an unvalidated site cannot be Open/Prioritize.
    """
    score = area_score if area_score is not None else opportunity_score
    if score is None:
        decision = WATCH
    elif score >= 80:
        decision = OPEN
    elif score >= 60:
        decision = TEST
    elif score >= 50:
        decision = TEST if (demand_score or 0) >= 75 else WATCH
    else:
        decision = WATCH if (demand_score or 0) >= 70 else AVOID

    if not site_validated and decision == OPEN:
        decision = TEST
    if readiness == "Weak" and decision == OPEN:
        decision = _downgrade(decision)
    if (confidence_score is not None and confidence_score < 50
            and decision in (OPEN, TEST)):
        decision = _downgrade(decision)
    if score is not None and score < 60 and decision == OPEN:
        decision = TEST
    return decision


def _city(profile: Dict[str, Any]) -> str:
    city = profile.get("city") or ""
    state = profile.get("state") or ""
    return ", ".join(x for x in (city, state) if x) or city or state


def _location_name(profile: Dict[str, Any]) -> str:
    anchor = profile.get("anchor") or {}
    anchor_name = anchor.get("name") or ""
    candidate_name = profile.get("candidate_name") or profile.get("name") or ""
    if "needs commercial site validation" in candidate_name.lower() and anchor_name:
        return f"Near {anchor_name}"
    return candidate_name or (f"Near {anchor_name}" if anchor_name else "Unknown location")


def _address(profile: Dict[str, Any]) -> str:
    anchor = profile.get("anchor") or {}
    return anchor.get("formatted_address") or profile.get("formatted_address") or ""


def _best_course(perf: Dict[str, Any]) -> Dict[str, Any]:
    recs = ((perf.get("center_opening") or {}).get("recommendations") or [])
    usable = [r for r in recs if r.get("decision") != AVOID]
    pool = usable or recs
    if not pool:
        bench = perf.get("course_enrollment_benchmarks") or {}
        best_key = bench.get("strongest_historical_course_type_key")
        best_label = bench.get("strongest_historical_course_type")
        if best_key or best_label:
            return {
                "course_type": best_key,
                "label": best_label or best_key,
                "opportunity_score": None,
                "reasons": [],
            }
        graph = (perf.get("evaluation_graph") or {}).get("course_opportunity_graph") or []
        pool = graph
    if not pool:
        return {}

    top_score = max(
        _num(r.get("opportunity_score") or r.get("final_score")) or -1
        for r in pool
    )
    benchmarks = _benchmark_by_label(perf)
    benchmark_supported = []
    for rec in pool:
        score = _num(rec.get("opportunity_score") or rec.get("final_score"))
        if score is None or top_score - score > 5:
            continue
        bench = (
            benchmarks.get(str(rec.get("label") or ""))
            or benchmarks.get(str(rec.get("course_type") or ""))
        )
        if not bench:
            continue
        diff = _num(bench.get("difference_vs_allcpr_average"))
        avg = _num(bench.get("average_students_per_class"))
        if diff is not None and diff > 0:
            benchmark_supported.append((avg or 0.0, diff, score, rec))
    if benchmark_supported:
        benchmark_supported.sort(reverse=True, key=lambda item: item[:3])
        return benchmark_supported[0][3]

    return max(pool, key=lambda r: _num(r.get("opportunity_score")
                                       or r.get("final_score")) or -1)


def _benchmark_by_label(perf: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    bench = perf.get("course_enrollment_benchmarks") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in bench.get("course_benchmarks") or []:
        if row.get("course_type"):
            out[str(row["course_type"])] = row
        if row.get("course_type_key"):
            out[str(row["course_type_key"])] = row
    return out


def _course_reasons(best_course: Dict[str, Any]) -> List[str]:
    reasons = list(best_course.get("reasons") or [])[:2]
    label = best_course.get("label") or best_course.get("course_type")
    score = _fmt_score(best_course.get("opportunity_score")
                       or best_course.get("final_score"))
    if label and score is not None and not reasons:
        reasons.append(f"{label} is the strongest course opportunity ({score}/100).")
    return reasons


def _main_reasons(profile: Dict[str, Any], scored: Dict[str, Any],
                  best_course: Dict[str, Any],
                  perf: Optional[Dict[str, Any]] = None) -> List[str]:
    subs = scored.get("sub_scores") or {}
    reasons: List[str] = []
    demand_score = _fmt_score(subs.get("demand_score"))
    if demand_score is not None and demand_score >= 75:
        reasons.append("Strong nearby healthcare / training demand signals.")
    elif demand_score is not None:
        reasons.append(f"Public demand signals are present ({demand_score}/100).")
    reasons.extend(_course_reasons(best_course))
    benchmarks = _benchmark_by_label(perf or {})
    benchmark_payload = (perf or {}).get("course_enrollment_benchmarks") or {}
    bench = (
        benchmarks.get(str(best_course.get("label") or ""))
        or benchmarks.get(str(best_course.get("course_type") or ""))
    )
    if bench and bench.get("difference_vs_allcpr_average") is not None:
        diff = float(bench["difference_vs_allcpr_average"])
        if diff > 0:
            reasons.append(
                f"{bench.get('course_type')} historically beats ALLCPR average "
                f"by {diff:.2f} students/class."
            )
        elif diff < 0:
            reasons.append(
                f"{bench.get('course_type')} is below ALLCPR average historically; "
                "launch only if local demand supports it."
            )
    leader = benchmark_payload.get("strongest_historical_course_type")
    if leader:
        reasons.append(f"Historical benchmark leader: {leader}.")
    sched = (((profile.get("course_performance") or {})
              .get("schedule_intelligence") or {})
             .get("best_day") or {})
    if not sched:
        sched = (((scored.get("historical_performance_score") or {})
                  if isinstance(scored.get("historical_performance_score"), dict) else {})
                 .get("best_day") or {})
    label = sched.get("label") if isinstance(sched, dict) else None
    if label:
        reasons.append(f"{label} classes appear stronger in historical scheduling data.")
    if not reasons:
        reasons.append("Area has enough signal to merit a structured business review.")
    return reasons[:5]


def _main_risks(scored: Dict[str, Any], profile: Dict[str, Any],
                area_score: Optional[float]) -> List[str]:
    risks: List[str] = []
    if not _site_validated(scored):
        risks.append("Site is not validated.")
        risks.append("No confirmed commercial storefront / leasing opportunity.")
    comp = scored.get("competition_detail") or {}
    pressure = str(comp.get("competition_pressure_band") or "").lower()
    if pressure in {"high", "extreme"}:
        risks.append("Competition-heavy area.")
    elif _fmt_score((scored.get("sub_scores") or {}).get("competition_gap_score")) == 0:
        risks.append("Competition gap is weak.")
    if area_score is not None and area_score < 70:
        risks.append("Area score is moderate, not strong.")
    if not risks:
        risks.append("Future enrollment still depends on execution and local competition.")
    return risks[:5]


def _decision_reason(decision: str, site_validated: bool,
                     area_score: Optional[float]) -> str:
    if decision == OPEN:
        return "Strong score and validated site readiness support prioritizing this opening."
    if not site_validated and (area_score or 0) >= 55:
        return "Promising demand area, but not lease-ready."
    if decision == TEST:
        return "Promising signals, but validate demand and site economics before leasing."
    if decision == WATCH:
        return "Some useful signal, but score or readiness is not strong enough yet."
    return "Current score, readiness, or risk profile does not justify testing now."


def _next_action(decision: str, site_validated: bool) -> str:
    if decision == OPEN:
        return "Prioritize lease negotiation and schedule the first course cohort."
    if not site_validated:
        return ("Validate parking, rent, and classroom availability. Run a small "
                "paid-search or landing-page demand test before signing any lease.")
    if decision == TEST:
        return "Run a limited pilot class schedule and compare fill rate against target."
    if decision == WATCH:
        return "Keep monitoring enrollment, rent, and competitor movement before testing."
    return "Do not spend opening budget here until demand or readiness improves."


def _evidence_summary(area_score: Optional[float], opportunity_score: Optional[float],
                      confidence: str, readiness: str,
                      site_status: str, course_label: str) -> str:
    parts = []
    if area_score is not None:
        parts.append(f"Area score {area_score}/100")
    if opportunity_score is not None and course_label:
        parts.append(f"{course_label} opportunity {opportunity_score}/100")
    parts.append(confidence)
    parts.append(f"readiness {readiness.lower()}")
    parts.append(site_status.lower())
    return "; ".join(parts) + "."


def build_center_recommendations_from_report(
    payload: Dict[str, Any],
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Build recommendation rows from a scored JSON report payload."""
    candidates = list(payload.get("candidates") or [])
    if limit is not None:
        candidates = candidates[:limit]
    perf = ((payload.get("context") or {}).get("course_performance") or {})
    best_course = _best_course(perf)
    course_label = best_course.get("label") or best_course.get("course_type") or ""
    opportunity_score = _fmt_score(best_course.get("opportunity_score")
                                   or best_course.get("final_score"))

    rows: List[Dict[str, Any]] = []
    for item in candidates:
        profile = item.get("profile") or {}
        scored = item.get("scored") or {}
        subs = scored.get("sub_scores") or {}
        area_score = _fmt_score(scored.get("area_score")
                                or scored.get("ranking_score"))
        confidence_score = _fmt_score(subs.get("confidence_score"))
        confidence_label = data_confidence_label(confidence_score)
        validated = _site_validated(scored)
        readiness = expansion_readiness(scored, area_score)
        validation_status = site_validation_status(scored, profile)
        decision = decide_center_opening(
            area_score,
            opportunity_score=opportunity_score,
            confidence_score=confidence_score,
            readiness=readiness,
            site_validated=validated,
            demand_score=_fmt_score(subs.get("demand_score")),
        )
        reasons = _main_reasons(profile, scored, best_course, perf=perf)
        risks = _main_risks(scored, profile, area_score)
        rows.append({
            "city": _city(profile),
            "location_name": _location_name(profile),
            "address": _address(profile),
            "course_type": course_label,
            "area_score": area_score,
            "opportunity_score": opportunity_score,
            "confidence_score": confidence_score,
            "data_confidence_label": confidence_label,
            "expansion_readiness": readiness,
            "site_validation_status": validation_status,
            "decision_label": decision,
            "decision_reason": _decision_reason(decision, validated, area_score),
            "main_reasons": reasons,
            "main_risks": risks,
            "suggested_next_action": _next_action(decision, validated),
            "evidence_summary": _evidence_summary(
                area_score, opportunity_score, confidence_label, readiness,
                validation_status, course_label,
            ),
            "warning_note": WARNING_NOTE,
        })

    return {
        "warning_note": WARNING_NOTE,
        "n": len(rows),
        "recommendations": rows,
    }


def write_recommendations_json(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return " | ".join(str(v) for v in value)
    return value


def write_recommendations_csv(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rec in payload.get("recommendations") or []:
            writer.writerow({k: _csv_value(rec.get(k)) for k in CSV_COLUMNS})


def format_terminal_summary(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    for idx, rec in enumerate(payload.get("recommendations") or [], start=1):
        city = rec.get("city") or "Unknown city"
        location = rec.get("location_name") or "Unknown location"
        lines.append(f"#{idx} {city} — {location}")
        lines.append(f"Decision: {rec.get('decision_label')}")
        lines.append(f"Score: {rec.get('area_score')}")
        lines.append(f"Confidence: {rec.get('data_confidence_label')}")
        lines.append(f"Readiness: {rec.get('expansion_readiness')}")
        lines.append(f"Reason: {rec.get('decision_reason')}")
        lines.append(f"Next action: {rec.get('suggested_next_action')}")
        lines.append("")
    if not lines:
        lines.append("No center-opening recommendations available.")
    return "\n".join(lines).rstrip()
