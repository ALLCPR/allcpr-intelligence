"""
Review complaint-theme detection.

Average ratings tell you competitors are weak (SF CPR providers average ~2.0
on Yelp); they don't tell you *why*. This module reads review excerpts and
clusters the recurring complaints into actionable themes — parking,
scheduling, instructor quality, wait times, refunds, booking friction,
price, customer service — so the report can say "open with online booking +
validated parking + senior instructors" instead of just "competitors are
weak."

Deterministic and dependency-free: regex theme patterns scored against
*negative* reviews only (rating ≤ 3), consistent with the project's
no-fabrication ethos. A positive review mentioning "parking was easy" is
not counted as a parking complaint.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

# Only reviews at or below this star rating are scanned for complaints.
NEGATIVE_RATING_MAX = 3

# theme -> compiled pattern. Because we only scan reviews rated <= 3 stars,
# the presence of a topic keyword in an already-negative review is a strong
# complaint signal — so the patterns favor recall (catching natural phrasing
# like "always crowded", "long line", "wouldn't refund") over rigid
# complaint grammar. Precision comes from the negative-rating gate.
_THEME_PATTERNS: Dict[str, re.Pattern] = {
    "parking": re.compile(r"\bpark(ing|ed)?\b", re.I),
    "scheduling": re.compile(
        r"\b(reschedul\w*|cancel\w*|schedul\w*|booking|booked|appointment|"
        r"enroll\w*|orientation|sign[- ]?up|wait[- ]?list|availability|"
        r"no openings|fully booked)\b", re.I),
    "instructor_quality": re.compile(
        r"\b(instructor|teacher|trainer|rude|unprofessional|unprepared|"
        r"disorganiz\w*|condescending|incompetent|dismissive|"
        r"didn'?t care|not helpful|unqualified)\b", re.I),
    "wait_time": re.compile(
        r"\b(wait\w*|crowded|long line|in line|packed|slow|took (forever|"
        r"too long|hours)|hour(s)? (of )?wait)\b", re.I),
    "refund": re.compile(
        r"\b(refund\w*|money back|non-?refundable|charged|overcharg\w*|"
        r"keep(s)? your money|won'?t (give|return) (my )?money)\b", re.I),
    "booking_ux": re.compile(
        r"\b(website|online (booking|registration|sign)|web ?site|"
        r"app (was )?(broken|confusing)|couldn'?t (book|register) online|"
        r"portal)\b", re.I),
    "price": re.compile(
        r"\b(overpriced|expensive|pricey|too much money|rip[- ]?off|"
        r"not worth|waste of money|cost too much)\b", re.I),
    "customer_service": re.compile(
        r"\b(rude|unhelpful|horrible|worst|terrible|awful|avoid|"
        r"unresponsive|never (called|emailed|got) back|hung up|"
        r"no one (answered|responded|helped)|disrespect\w*)\b", re.I),
    "certification_issues": re.compile(
        r"\b(certif\w*|(my )?card (never|didn'?t)|expired|"
        r"not (AHA|American Heart Association)|didn'?t get (my )?"
        r"(card|certificate)|invalid)\b", re.I),
}

_THEME_LABELS = {
    "parking": "Parking",
    "scheduling": "Scheduling / availability",
    "instructor_quality": "Instructor quality",
    "wait_time": "Wait times",
    "refund": "Refund handling",
    "booking_ux": "Online booking / website",
    "price": "Pricing",
    "customer_service": "Customer service",
    "certification_issues": "Certification turnaround",
}

# How ALLCPR could turn each complaint into a positioning advantage.
_THEME_OPPORTUNITY = {
    "parking": "Pick a site with validated/free parking and advertise it.",
    "scheduling": "Offer flexible reschedule + a published live-availability calendar.",
    "instructor_quality": "Lead with experienced, vetted AHA instructors.",
    "wait_time": "Run on-time small-cohort classes; promise no waiting.",
    "refund": "Offer a clear, friction-free refund / reschedule policy.",
    "booking_ux": "Ship a clean one-click online booking flow.",
    "price": "Transparent pricing with a clear value story (not a race to cheap).",
    "customer_service": "Same-day responsiveness; a real human answers.",
    "certification_issues": "Same-day / digital certification turnaround.",
}


@dataclass
class MarketFrustrations:
    reviews_scanned: int
    negative_reviews: int
    theme_counts: Dict[str, int] = field(default_factory=dict)
    top_frustrations: List[Dict[str, object]] = field(default_factory=list)
    data_confidence: str = "low"


def detect_themes_in_text(text: str) -> List[str]:
    """Return the complaint themes present in one piece of text."""
    hits: List[str] = []
    for theme, pattern in _THEME_PATTERNS.items():
        if pattern.search(text or ""):
            hits.append(theme)
    return hits


def analyze_reviews(reviews: List[Dict[str, object]]) -> MarketFrustrations:
    """Aggregate complaint themes across a list of review-excerpt dicts.

    Only reviews with ``rating <= NEGATIVE_RATING_MAX`` are scanned. Each
    review contributes at most one count per theme.
    """
    theme_counter: Counter = Counter()
    example_by_theme: Dict[str, str] = {}
    negative = 0
    for rv in reviews:
        rating = rv.get("rating")
        text = str(rv.get("text") or "")
        if isinstance(rating, (int, float)) and rating > NEGATIVE_RATING_MAX:
            continue
        if isinstance(rating, (int, float)):
            negative += 1
        for theme in detect_themes_in_text(text):
            theme_counter[theme] += 1
            example_by_theme.setdefault(theme, text.strip()[:160])

    total = len(reviews)
    if total == 0:
        confidence = "low"
    elif total >= 12:
        confidence = "high"
    elif total >= 5:
        confidence = "medium"
    else:
        confidence = "low"

    top = [
        {
            "theme": theme,
            "label": _THEME_LABELS.get(theme, theme),
            "count": count,
            "opportunity": _THEME_OPPORTUNITY.get(theme, ""),
            "example": example_by_theme.get(theme, ""),
        }
        for theme, count in theme_counter.most_common()
    ]
    return MarketFrustrations(
        reviews_scanned=total,
        negative_reviews=negative,
        theme_counts=dict(theme_counter),
        top_frustrations=top,
        data_confidence=confidence,
    )


def summarize_positioning(frustrations: MarketFrustrations,
                          top_n: int = 3) -> str:
    """One-line positioning hint from the dominant complaint themes."""
    if not frustrations.top_frustrations:
        if frustrations.reviews_scanned == 0:
            return ("No competitor reviews analyzed — run with "
                    "--analyze-reviews and a Yelp key to surface "
                    "competitor weaknesses.")
        return ("No recurring complaints found in the analyzed reviews — "
                "differentiate on price, partnerships, or convenience.")
    labels = [f["label"].lower() for f in frustrations.top_frustrations[:top_n]]
    return ("Competitors are most criticized for "
            + ", ".join(labels)
            + " — lead with the opposite.")
