"""
Staff-ready search queries for turning ZIP signals into real leads.

Live scraping is not implemented yet; this is the manual bridge. For a ZIP
(optionally with a staff-typed city/state, since enrichment rows carry no
place name) it generates the exact Google / LinkedIn searches a staff member
should run to find named instructor candidates and specific rentable rooms.
Anything found this way must be entered as SIGNAL_ONLY / NEEDS_VERIFICATION —
a search hit is never a certification.
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote_plus

GOOGLE_URL = "https://www.google.com/search?q={q}"
LINKEDIN_URL = "https://www.linkedin.com/search/results/people/?keywords={q}"

# (query template, what the staff member is looking for)
_INSTRUCTOR_QUERIES = (
    ('site:.edu nursing faculty BLS instructor {place}',
     "Nursing faculty with BLS instructor background"),
    ('"AHA BLS Instructor" "{place}"',
     "People publicly identifying as AHA BLS instructors"),
    ('"CPR instructor" "{place}"',
     "CPR instructors of any affiliation"),
    ('"Red Cross" instructor CPR OR BLS "{place}"',
     "Red Cross instructor signals"),
    ('"BLS instructor" OR "CPR instructor" jobs "{place}"',
     "Instructor job postings — applicants and posters are both leads"),
    ('EMT program instructor "{place}"',
     "EMT program instructors"),
    ('paramedic OR "fire academy" instructor "{place}"',
     "Paramedic / fire-academy instructors"),
    ('hospital "clinical educator" OR "education department" "{place}"',
     "Hospital clinical educators"),
    ('CPR training business "{place}"',
     "Existing CPR businesses — owners/instructors may teach for ALLCPR"),
)

_SPACE_QUERIES = (
    ('"meeting room rental" "{place}"',
     "General meeting-room rentals"),
    ('"coworking" meeting room "{place}"',
     "Coworking spaces with bookable rooms"),
    ('Regus OR "business center" meeting room "{place}"',
     "Business-center rooms (Regus-style)"),
    ('peerspace OR liquidspace training room "{place}"',
     "Marketplace listings (Peerspace/LiquidSpace-style)"),
    ('hotel meeting room rental "{place}"',
     "Hotel meeting rooms"),
    ('community center room rental "{place}"',
     "Community-center rooms"),
    ('church hall rental "{place}"',
     "Church halls"),
    ('library meeting room reserve "{place}"',
     "Library meeting rooms"),
    ('college OR "adult school" facility rental "{place}"',
     "College / adult-school room rentals"),
    ('"training room rental" "{place}"',
     "Dedicated training rooms"),
)


def _build(queries, place: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for template, purpose in queries:
        q = template.format(place=place)
        out.append({
            "query": q,
            "purpose": purpose,
            "url": GOOGLE_URL.format(q=quote_plus(q)),
        })
    return out


def generate_search_queries(zip_code: str, city: str = "",
                            state: str = "") -> Dict[str, Any]:
    """Instructor + room search queries for one ZIP.

    ``city``/``state`` are optional staff input; with them the queries target
    the place name (much better hit rate), otherwise they fall back to the
    ZIP itself.
    """
    zip_code = str(zip_code).zfill(5)
    place = " ".join(p for p in (city.strip(), state.strip()) if p) or zip_code
    linkedin_q = f"CPR BLS instructor {place}"
    return {
        "zip": zip_code,
        "place_used": place,
        "instructor_queries": _build(_INSTRUCTOR_QUERIES, place),
        "space_queries": _build(_SPACE_QUERIES, place),
        "linkedin_people_search": {
            "query": linkedin_q,
            "url": LINKEDIN_URL.format(q=quote_plus(linkedin_q)),
        },
        "note": ("Run these manually and record findings as leads. A search "
                 "hit is a signal — credentials must be verified by staff "
                 "before anyone is treated as certified."
                 + ("" if city else " Tip: pass the city name for much "
                    "better results than a bare ZIP.")),
    }
