"""
Instructor Supply Discovery + readiness scoring.

Not a "professor finder": professors are one candidate type among many
(existing ALLCPR instructors, AHA/ARC signal leads, EMT/paramedic/fire-academy
instructors, hospital educators, CPR business owners, healthcare-education
programs). Sources are graded honestly:

    VERIFIED            human recorded a credential verification
    NEEDS_VERIFICATION  a named person with claimed credentials (e.g. roster)
    SIGNAL_ONLY         institutional signal (a nursing school exists here);
                        no named, contactable instructor yet

Discovery here is offline: it combines the imported ALLCPR roster with the
institutional signals already present in the enriched ZIP record (nursing /
EMT school counts, hospital counts, competitor counts). Live public-source
scraping can be layered in later; anything it finds must also enter as
SIGNAL_ONLY / NEEDS_VERIFICATION, never as certified.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import DATA_DIR
from app.ops.imports import load_instructors_import
from app.ops.models import (
    AHA_BLS,
    ARC_BLS,
    ARC_CPR_FA_AED,
    COURSE_TYPES,
    STAGE_BLOCKED,
    STAGE_CANDIDATE_FOUND,
    STAGE_CONFIRMED,
    STAGE_NO_SIGNAL,
    STAGE_SIGNAL_ONLY,
    InstructorCandidate,
    band_label,
)
from app.utils.geo_utils import haversine_miles

ZIP_CENTROIDS_FILE = DATA_DIR / "reference" / "zip_centroids.csv"

# Signal keywords → (candidate strength bonus, reason). Mirrors the spec's
# scoring guidance: explicit BLS/CPR instruction ranks above adjacent
# credentials, which rank above generic healthcare employment.
_STRONG_KEYWORDS = (
    "bls instructor", "cpr instructor", "aha instructor", "red cross instructor",
    "arc instructor", "bls", "cpr",
)
_SUPPORTING_KEYWORDS = (
    "aha", "american heart", "red cross", "acls", "pals", "emt", "paramedic",
    "nursing", "simulation lab", "clinical education", "first aid",
)

# How far an instructor can reasonably be from the target ZIP before the lead
# stops counting toward readiness (matches SOP travel-reimbursement reality:
# beyond ~20 mi one way travel gets expensive).
DEFAULT_TRAVEL_RADIUS_MILES = 20.0

_zip_centroid_cache: Optional[Dict[str, Tuple[float, float]]] = None


def _zip_centroids() -> Dict[str, Tuple[float, float]]:
    """ZIP → (lat, lng) for the whole country.

    The small reference CSV (curated Bay Area sample) loads first, then the
    national modeled layer (data/processed/national_demand_lite.json.gz,
    ~33.7k ZIPs with lat/lon — tracked in git, so present on Render) fills in
    everything else. Without the fallback, instructor→ZIP distance only worked
    inside the Bay Area sample and "find instructors near any ZIP" broke.
    """
    global _zip_centroid_cache
    if _zip_centroid_cache is None:
        table: Dict[str, Tuple[float, float]] = {}
        try:
            with Path(ZIP_CENTROIDS_FILE).open("r", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    try:
                        table[str(row["zip"]).zfill(5)] = (
                            float(row["lat"]), float(row["lng"]))
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError:
            table = {}
        # National fallback: every modeled ZIP in the country.
        try:
            import gzip
            import json
            from app.config import PROCESSED_DIR
            lite = PROCESSED_DIR / "national_demand_lite.json.gz"
            if lite.exists():
                with gzip.open(lite, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
                for row in data.get("rows") or []:
                    z = str(row.get("zip") or "").zfill(5)
                    if z in table:
                        continue
                    lat = row.get("lat")
                    lng = row.get("lon", row.get("lng"))
                    if lat is None or lng is None:
                        continue
                    try:
                        table[z] = (float(lat), float(lng))
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE0001 — fallback is best-effort
            pass
        _zip_centroid_cache = table
    return _zip_centroid_cache


def distance_between_zips(zip_a: str, zip_b: str) -> Optional[float]:
    """Centroid-to-centroid miles between two ZIPs; None when unknown."""
    table = _zip_centroids()
    a = table.get(str(zip_a).zfill(5))
    b = table.get(str(zip_b).zfill(5))
    if not a or not b:
        return None
    return round(haversine_miles(a, b), 1)


# --------------------------------------------------------------------------
# Candidate scoring
# --------------------------------------------------------------------------
def score_instructor_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``confidence_score`` (0..100) + ``score_reasons`` to a candidate.

    Higher when: explicit BLS/CPR instruction wording, adjacent credentials
    (AHA/ARC/ACLS/PALS/EMT/nursing/sim-lab), close to the ZIP, contactable,
    institutionally credible, and plausibly long-term — never for merely
    being a healthcare worker.
    """
    score = 0.0
    reasons: List[str] = []
    signals = " ".join(
        str(s).lower()
        for s in (candidate.get("certification_signals") or [])
    )
    title_org = " ".join((
        str(candidate.get("title") or ""),
        str(candidate.get("organization") or ""),
    )).lower()
    haystack = f"{signals} {title_org}"

    status = candidate.get("credential_status") or "UNKNOWN"
    if status == "VERIFIED":
        score += 40
        reasons.append("Credentials verified by ALLCPR staff")
    elif status == "NEEDS_VERIFICATION":
        score += 22
        reasons.append("Named candidate with claimed credentials "
                       "(needs verification)")
    elif status == "SIGNAL_ONLY":
        score += 8
        reasons.append("Institutional signal only — no named instructor yet")
    elif status == "EXPIRED":
        score += 10
        reasons.append("Credential expired — recertification path possible")
    elif status == "REJECTED":
        return {**candidate, "confidence_score": 0.0,
                "score_reasons": ["Rejected by staff"]}

    if any(k in haystack for k in _STRONG_KEYWORDS):
        score += 20
        reasons.append("Explicit BLS/CPR instruction signal")
    elif any(k in haystack for k in _SUPPORTING_KEYWORDS):
        score += 10
        reasons.append("Adjacent healthcare-training credential signal")

    if candidate.get("source") == "allcpr_internal_import":
        score += 15
        reasons.append("Existing ALLCPR instructor")

    distance = candidate.get("distance_miles")
    radius = candidate.get("travel_radius_miles") or DEFAULT_TRAVEL_RADIUS_MILES
    if distance is not None:
        if distance <= 10:
            score += 12
            reasons.append(f"Close to target ZIP ({distance} mi)")
        elif distance <= radius:
            score += 7
            reasons.append(f"Within travel radius ({distance} mi)")
        else:
            score -= 10
            reasons.append(f"Outside travel radius ({distance} mi)")

    if candidate.get("email") or candidate.get("phone"):
        score += 8
        reasons.append("Public/known contact info")
    if candidate.get("organization"):
        score += 5
        reasons.append("Institutionally credible affiliation")
    interest = candidate.get("long_term_interest") or "UNKNOWN"
    if interest == "YES":
        score += 8
        reasons.append("Open to long-term recurring teaching")
    elif interest == "MAYBE":
        score += 4
        reasons.append("Possible long-term interest")
    elif interest == "NO":
        score -= 8
        reasons.append("Not interested in long-term teaching")

    out = dict(candidate)
    out["confidence_score"] = round(max(0.0, min(100.0, score)), 1)
    out["score_reasons"] = reasons
    return out


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def _roster_candidate(row: Dict[str, Any], zip_code: str) -> Dict[str, Any]:
    courses = [c.upper().replace(" ", "_") for c in row.get("courses") or []]
    courses = [c for c in courses if c in COURSE_TYPES] or list(COURSE_TYPES[1:])
    if AHA_BLS in courses:
        ctype = "AHA_BLS_INSTRUCTOR"
    elif ARC_BLS in courses:
        ctype = "ARC_BLS_INSTRUCTOR"
    else:
        ctype = "ARC_CPR_INSTRUCTOR"
    cand = InstructorCandidate(
        name=row.get("name", ""),
        source="allcpr_internal_import",
        organization="ALLCPR",
        email=row.get("email", ""),
        phone=row.get("phone", ""),
        city=row.get("city", ""),
        state=row.get("state", ""),
        zip=row.get("zip", ""),
        distance_miles=distance_between_zips(row.get("zip", ""), zip_code),
        candidate_type=ctype,
        certification_signals=list(row.get("certifications") or []),
        # Roster rows are still claims until a human marks them verified.
        verified_certifications=(
            list(row.get("certifications") or []) if row.get("verified") else []),
        credential_status=(
            "VERIFIED" if row.get("verified") else "NEEDS_VERIFICATION"),
        courses_possible=courses,
        travel_radius_miles=row.get("travel_radius_miles"),
        availability_notes=row.get("availability", ""),
        long_term_interest=(
            row.get("long_term_interest")
            if row.get("long_term_interest") in ("YES", "NO", "MAYBE")
            else "UNKNOWN"),
        rate_notes=row.get("pay_rate", ""),
        notes=row.get("reliability_notes", ""),
    ).to_dict()
    return cand


# Institutional signals from the enriched ZIP record → SIGNAL_ONLY leads.
# (zip_row key, minimum count, candidate_type, courses, lead name template)
_ZIP_SIGNAL_SOURCES = (
    ("nursing_school_count", 1, "NURSING_PROFESSOR",
     [ARC_BLS, ARC_CPR_FA_AED, AHA_BLS],
     "Nursing program faculty ({count} nursing school(s) in ZIP)"),
    ("training_school_count", 1, "EMT_INSTRUCTOR",
     [AHA_BLS, ARC_BLS, ARC_CPR_FA_AED],
     "EMT / healthcare training school instructors ({count} school(s) in ZIP)"),
    ("health_program_school_count", 1, "UNKNOWN_HEALTHCARE_TRAINER",
     [ARC_BLS, ARC_CPR_FA_AED],
     "Health-science program educators ({count} program(s) in ZIP)"),
    ("hospital_count", 1, "HOSPITAL_EDUCATOR",
     [AHA_BLS, ARC_BLS],
     "Hospital clinical educators ({count} hospital(s) in ZIP)"),
    ("competitor_count", 1, "CPR_BUSINESS_OWNER",
     [AHA_BLS, ARC_BLS, ARC_CPR_FA_AED],
     "Instructors at {count} existing CPR training business(es) in ZIP"),
)


def _signal_candidates(zip_code: str, zip_row: Dict[str, Any]
                       ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key, minimum, ctype, courses, name_tpl in _ZIP_SIGNAL_SOURCES:
        raw = zip_row.get(key)
        try:
            count = int(float(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            count = 0
        if count < minimum:
            continue
        out.append(InstructorCandidate(
            name=name_tpl.format(count=count),
            source="zip_enrichment_signal",
            zip=zip_code,
            distance_miles=0.0,
            candidate_type=ctype,
            certification_signals=[f"{key}={count}"],
            credential_status="SIGNAL_ONLY",
            courses_possible=list(courses),
            notes=("Institutional lead from enrichment data — identify and "
                   "contact named instructors, then verify credentials."),
        ).to_dict())
    return out


def discover_instructor_candidates(
    zip_code: str,
    zip_row: Optional[Dict[str, Any]] = None,
    roster: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build the scored instructor-candidate list for one ZIP.

    Sources: imported ALLCPR roster (within travel radius) + institutional
    signals from the enriched ZIP record. Sorted by confidence, best first.
    """
    zip_code = str(zip_code).zfill(5)
    if roster is None:
        roster = load_instructors_import()
    candidates: List[Dict[str, Any]] = []
    for row in roster:
        cand = _roster_candidate(row, zip_code)
        distance = cand.get("distance_miles")
        radius = cand.get("travel_radius_miles") or DEFAULT_TRAVEL_RADIUS_MILES
        # Same-ZIP roster rows always count even without centroid data.
        if distance is None and cand.get("zip") != zip_code:
            continue
        if distance is not None and distance > radius * 1.5:
            continue
        candidates.append(cand)
    if zip_row:
        candidates.extend(_signal_candidates(zip_code, zip_row))
    scored = [score_instructor_candidate(c) for c in candidates]
    scored.sort(key=lambda c: c.get("confidence_score") or 0.0, reverse=True)
    return scored


def group_by_course(candidates: List[Dict[str, Any]]
                    ) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {c: [] for c in COURSE_TYPES}
    for cand in candidates:
        for course in cand.get("courses_possible") or []:
            if course in grouped:
                grouped[course].append(cand)
    return grouped


def top_instructor_leads(candidates: List[Dict[str, Any]], limit: int = 5,
                         course: Optional[str] = None
                         ) -> List[Dict[str, Any]]:
    pool = candidates
    if course:
        pool = [c for c in candidates
                if course in (c.get("courses_possible") or [])]
    return pool[:limit]


# --------------------------------------------------------------------------
# Readiness score (per the operational ladder)
# --------------------------------------------------------------------------
def instructor_readiness_score(candidates: List[Dict[str, Any]],
                               course: Optional[str] = None
                               ) -> Dict[str, Any]:
    """Readiness ladder:

    100 confirmed instructor ready to teach
     80 existing ALLCPR instructor nearby
     65 strong candidates found, not contacted
     50 supply signals exist but no direct candidate
     25 weak signals only
      0 no instructor path found
    """
    pool = candidates
    if course:
        pool = [c for c in candidates
                if course in (c.get("courses_possible") or [])]
    usable = [c for c in pool if c.get("outreach_status") != "REJECTED"
              and c.get("credential_status") != "REJECTED"]

    def _within_radius(c: Dict[str, Any]) -> bool:
        d = c.get("distance_miles")
        radius = c.get("travel_radius_miles") or DEFAULT_TRAVEL_RADIUS_MILES
        return d is None or d <= radius

    confirmed = [
        c for c in usable
        if c.get("credential_status") == "VERIFIED"
        and c.get("outreach_status") in ("CONFIRMED", "AVAILABLE",
                                         "CREDENTIAL_VERIFIED")
        and _within_radius(c)
    ]
    allcpr_nearby = [
        c for c in usable
        if c.get("source") == "allcpr_internal_import" and _within_radius(c)
    ]
    named = [c for c in usable
             if c.get("credential_status") in ("NEEDS_VERIFICATION", "VERIFIED")
             and c.get("name") and _within_radius(c)]
    strong_named = [c for c in named if (c.get("confidence_score") or 0) >= 40]
    signals = [c for c in usable if c.get("credential_status") == "SIGNAL_ONLY"]

    if confirmed:
        score, reason = 100.0, (
            f"{len(confirmed)} confirmed instructor(s) ready to teach")
    elif allcpr_nearby:
        score, reason = 80.0, (
            f"{len(allcpr_nearby)} existing ALLCPR instructor(s) nearby "
            "(availability to confirm)")
    elif strong_named:
        score, reason = 65.0, (
            f"{len(strong_named)} strong instructor candidate(s) found, "
            "not yet contacted/verified")
    elif len(signals) >= 2:
        score, reason = 50.0, (
            "Instructor supply signals exist (schools/hospitals/competitors) "
            "but no direct candidate confirmed")
    elif signals or named:
        score, reason = 25.0, "Weak instructor signals only"
    else:
        score, reason = 0.0, "No instructor path found"

    # Lead stage: what actually backs the score (a signal-backed 50 must not
    # read like a found instructor).
    if confirmed:
        stage = STAGE_CONFIRMED
    elif named or allcpr_nearby:
        stage = STAGE_CANDIDATE_FOUND
    elif signals:
        stage = STAGE_SIGNAL_ONLY
    elif pool and not usable:
        stage = STAGE_BLOCKED
    else:
        stage = STAGE_NO_SIGNAL

    return {
        "score": score,
        "label": band_label(score),
        "stage": stage,
        "reason": reason,
        "counts": {
            "confirmed": len(confirmed),
            "allcpr_nearby": len(allcpr_nearby),
            "named_candidates": len(named),
            "signal_leads": len(signals),
        },
    }


def instructor_readiness_by_course(candidates: List[Dict[str, Any]]
                                   ) -> Dict[str, Dict[str, Any]]:
    return {course: instructor_readiness_score(candidates, course=course)
            for course in COURSE_TYPES}
