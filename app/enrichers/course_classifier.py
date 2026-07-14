"""
Deterministic course-type classifier (STEP 3).

Canonical home for ALLCPR's course-type taxonomy and the rule-based classifier
that maps a messy Enrollware class name onto a small fixed catalog. The function
is **pure, deterministic, and LLM-free**: the same class name always yields the
same course-type key, so reports are reproducible and auditable.

``app.collectors.enrollware`` re-exports ``COURSE_TYPE_LABELS`` and
``classify_course_type`` from this module, so existing imports keep working.

Design rules (consistent with the rest of the pipeline):
  - **Never guess.** A class we cannot confidently classify becomes
    ``unknown_course_type`` rather than being forced into a bucket.
  - **Word-boundary course tokens.** A company name like "AllCPR Group
    Training" must NOT register a "cpr" course (the substring inside "AllCPR").
"""
from __future__ import annotations

import re
from typing import Any, Dict

# Stable key -> human label. Order here is the canonical display order used by
# the reports. Keys are the contract; labels are presentation-only.
COURSE_TYPE_LABELS: Dict[str, str] = {
    "arc_cpr": "ARC CPR",
    "arc_bls": "ARC BLS",
    "aha_bls": "AHA BLS",
    "aha_cpr": "AHA CPR / Heartsaver",
    "allcpr_bls": "ALLCPR BLS",
    "allcpr_cpr": "ALLCPR CPR",
    "acls": "ACLS",
    "pals": "PALS",
    "cpr_first_aid_blended": "CPR / First Aid (hybrid)",
    "skills_session": "Skills session",
    "unknown_course_type": "ALLCPR",
}

# Plain-English definition of the hybrid course type, surfaced in the report
# wherever the "CPR / First Aid (hybrid)" label appears.
HYBRID_COURSE_KEY = "cpr_first_aid_blended"
HYBRID_COURSE_NOTE = (
    "Hybrid = student completes online CPR/First Aid lessons, then attends "
    "in-person skills practice/testing."
)


def _norm(text: Any) -> str:
    return str(text or "").strip().lower()


def _detect_provider(name: str) -> str:
    """Return 'arc' | 'aha' | 'allcpr' | '' from a normalized class name."""
    if any(tok in name for tok in ("allcpr", "all cpr", "all-cpr")):
        return "allcpr"
    if any(tok in name for tok in ("arc", "red cross", "redcross",
                                   "american red cross")):
        return "arc"
    if any(tok in name for tok in ("aha", "american heart", "heartsaver",
                                   "heartcode", "heart association")):
        return "aha"
    return ""


def classify_course_type(class_name: Any) -> str:
    """Map a messy Enrollware class name onto a canonical course-type key.

    Deterministic precedence (first match wins):
      1. Skills sessions — any "skills"/"skill session" class is its own
         performance bucket, regardless of provider or course.
      2. Advanced courses ACLS / PALS — provider-agnostic identifiers; these
         carry no CPR/BLS token, so they'd otherwise fall to "unknown".
      3. Provider + course wins: a Red Cross "First Aid/CPR/AED" class is
         ARC CPR, not the generic blended bucket; "Red Cross Basic Life
         Support" is ARC BLS; "AHA BLS Provider" is AHA BLS.
      4. Generic blended CPR/First Aid — only for provider-less combos
         (e.g. "Self Directed Adult First Aid/CPR/AED").
      5. Fallback to ``unknown_course_type`` — we never guess.

    Course tokens use word boundaries so a company name like "AllCPR Group
    Training" does NOT register a "cpr" course (the substring inside "AllCPR").
    """
    name = _norm(class_name)
    if not name:
        return "unknown_course_type"

    # 1. Skills / remediation sessions.
    if any(tok in name for tok in ("skills session", "skill session",
                                   "skills check", "skills test",
                                   "skills only", "rqi", "skills")):
        return "skills_session"

    # 2. Advanced provider-agnostic courses. Checked before the provider+course
    #    block: an "AHA ACLS Provider" class belongs in the ACLS bucket, not
    #    a BLS/CPR bucket (and it carries no BLS/CPR token anyway).
    if re.search(r"\bacls\b", name) or "advanced cardiac life support" in name \
            or "advanced cardiovascular life support" in name:
        return "acls"
    if re.search(r"\bpals\b", name) or "pediatric advanced life support" in name:
        return "pals"

    provider = _detect_provider(name)
    has_first_aid = bool(re.search(r"first\s*aid", name))
    has_cpr = bool(re.search(r"\bcpr\b", name)) or "heartsaver" in name
    has_bls = bool(re.search(r"\bbls\b", name)) or "basic life support" in name

    # 3. Provider + course wins over the generic blended bucket.
    if provider == "arc":
        if has_bls:
            return "arc_bls"
        if has_cpr or has_first_aid:
            return "arc_cpr"
    elif provider == "aha":
        if has_bls:
            return "aha_bls"
        if has_cpr or has_first_aid:
            return "aha_cpr"
    elif provider == "allcpr":
        if has_bls:
            return "allcpr_bls"
        if has_cpr:
            return "allcpr_cpr"

    # 4. Generic blended CPR / First Aid (no recognizable provider).
    if not has_bls and (
        "blend" in name
        or (has_first_aid and has_cpr)
        or (has_first_aid and "aed" in name)
    ):
        return "cpr_first_aid_blended"

    # 5. Unclassifiable.
    return "unknown_course_type"
