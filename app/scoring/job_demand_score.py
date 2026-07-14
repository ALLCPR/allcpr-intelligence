"""Job-posting certification demand score."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class JobDemandBreakdown:
    score: Optional[float]
    data_confidence: str
    active_postings_count: Optional[int]
    certification_postings_count: Optional[int]
    top_employers: List[Dict[str, object]]
    rationale: List[str]
    notes: str


def _norm_count(value: Optional[int], cap: int) -> float:
    if value is None or cap <= 0:
        return 0.0
    return min(1.0, max(0.0, value / cap))


def _int_or_none(value: object) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def compute_job_demand_score(job_demand: Dict[str, object]) -> JobDemandBreakdown:
    values = job_demand.get("values") or {}
    active = _int_or_none(values.get("active_postings_count"))
    cert = _int_or_none(values.get("certification_postings_count"))
    top_employers = list(job_demand.get("top_employers") or [])

    if active is None or cert is None:
        return JobDemandBreakdown(
            score=None,
            data_confidence="unknown",
            active_postings_count=None,
            certification_postings_count=None,
            top_employers=[],
            rationale=["job-posting certification demand unknown"],
            notes="No cited job-posting CSV data was provided.",
        )

    bls = _int_or_none(values.get("bls_count")) or 0
    cpr = _int_or_none(values.get("cpr_count")) or 0
    first_aid = _int_or_none(values.get("first_aid_count")) or 0
    advanced = (_int_or_none(values.get("acls_count")) or 0) + (
        _int_or_none(values.get("pals_count")) or 0
    )
    healthcare_roles = _int_or_none(values.get("healthcare_role_count")) or 0
    emt_roles = _int_or_none(values.get("emt_role_count")) or 0
    caregiver_roles = _int_or_none(values.get("caregiver_role_count")) or 0
    dental_roles = _int_or_none(values.get("dental_role_count")) or 0
    childcare_roles = _int_or_none(values.get("childcare_role_count")) or 0
    employers = _int_or_none(values.get("unique_employers_count")) or 0

    score_01 = (
        0.30 * _norm_count(cert, 25)
        + 0.16 * _norm_count(bls, 15)
        + 0.14 * _norm_count(cpr, 15)
        + 0.08 * _norm_count(first_aid, 12)
        + 0.07 * _norm_count(advanced, 8)
        + 0.10 * _norm_count(healthcare_roles + emt_roles, 25)
        + 0.08 * _norm_count(caregiver_roles + dental_roles + childcare_roles, 20)
        + 0.07 * _norm_count(employers, 10)
    )
    score = round(score_01 * 100, 2)

    rationale: List[str] = [
        f"{active} public job posting(s) supplied within override radius",
        f"{cert} posting(s) mention CPR/BLS/First Aid/AHA/Red Cross certification",
    ]
    if bls:
        rationale.append(f"{bls} posting(s) mention BLS")
    if cpr:
        rationale.append(f"{cpr} posting(s) mention CPR")
    if employers:
        rationale.append(f"{employers} unique employer(s) represented")

    return JobDemandBreakdown(
        score=score,
        data_confidence="manual_csv",
        active_postings_count=active,
        certification_postings_count=cert,
        top_employers=top_employers,
        rationale=rationale,
        notes="Derived from user-supplied cited public job-posting CSV rows.",
    )
