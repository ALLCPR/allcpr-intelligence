"""Safe competitor website signal analysis.

This module deliberately performs a tiny, respectful fetch:

  1. homepage only
  2. at most one obvious classes/schedule/booking page linked from homepage

It never crawls broadly, stores only derived signals, and treats network errors
as unknown rather than as weaknesses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from app.config import REQUEST_TIMEOUT
from app.utils.cache import Cache, cached_call
from app.utils.logging_utils import get_logger
from app.utils.report_safety import strip_sensitive_query_params
from app.utils.source_tracker import utcnow_iso

# 14-day TTL for competitor website signal payloads; aggressive enough that
# repeat runs are essentially free, conservative enough that a competitor
# launching online booking is picked up within ~2 weeks.
WEBSITE_ANALYSIS_TTL_SECONDS = 14 * 86400

logger = get_logger(__name__)

USER_AGENT = (
    "ALLCPR-Site-Intel/1.0 "
    "(single-page competitive research; contact site owner for concerns)"
)

SIGNALS = (
    "no_website",
    "online_booking",
    "class_schedule",
    "pricing",
    "multilingual_support",
    "contact_friction",
    "outdated_website",
    "certification_keywords",
    "acls_pals_offered",
    "group_corporate_offered",
    "weekend_classes_offered",
)

BOOKING_RE = re.compile(
    r"\b(book now|register now|register online|enroll now|sign up|"
    r"reserve (a )?(seat|spot)|checkout|cart|appointment|booking)\b",
    re.I,
)
SCHEDULE_RE = re.compile(
    r"\b(class schedule|course schedule|upcoming classes|training calendar|"
    r"calendar|schedule a class|schedule your class)\b",
    re.I,
)
PRICING_RE = re.compile(
    r"(\$\s?\d{2,4}|\b(pricing|prices|tuition|course fee|class fee|fees)\b)",
    re.I,
)
MULTILINGUAL_RE = re.compile(
    r"\b(español|espanol|spanish|中文|mandarin|cantonese|tagalog|vietnamese|"
    r"korean|arabic|hindi|multilingual|language)\b|hreflang=",
    re.I,
)
CONTACT_RE = re.compile(
    r"(mailto:|\btel:|\bcontact\b|\bcall\b|\bphone\b|"
    r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4})",
    re.I,
)
CERT_RE = re.compile(
    r"\b(CPR|BLS|AED|first aid|AHA|American Heart Association|"
    r"Red Cross|certification|certified|instructor)\b",
    re.I,
)
ACLS_PALS_RE = re.compile(
    r"\b(ACLS|PALS|advanced cardiac life support|"
    r"pediatric advanced life support)\b",
    re.I,
)
GROUP_CORPORATE_RE = re.compile(
    r"\b(group (training|classes|rates|booking)|corporate (training|"
    r"classes|programs)|on-?site (training|classes)|workplace training|"
    r"employer (training|programs)|company (training|programs))\b",
    re.I,
)
WEEKEND_RE = re.compile(
    r"\b(weekend (classes|courses|sessions|availability)|"
    r"saturday (class|classes|courses)|sunday (class|classes|courses)|"
    r"evening (classes|courses))\b",
    re.I,
)
OUTDATED_TEXT_RE = re.compile(r"\b(flash player|under construction|coming soon)\b", re.I)
YEAR_RE = re.compile(r"(?:copyright|©)?\s*(20\d{2})", re.I)

OBVIOUS_PAGE_WORDS = (
    "class", "classes", "schedule", "calendar", "course", "courses",
    "booking", "book", "register", "registration", "enroll", "pricing",
)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {k.lower(): v for k, v in attrs}
        self._href = attrs_dict.get("href")
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text_parts).strip()))
            self._href = None
            self._text_parts = []


@dataclass
class FetchResult:
    url: str
    text: str
    ok: bool
    error: str = ""


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return strip_sensitive_query_params(url)


def _is_non_public_test_domain(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return (
        host in {"localhost", "127.0.0.1", "::1"}
        or host.endswith(".example")
        or host.endswith(".test")
        or host.endswith(".invalid")
    )


def _fetch(session: requests.Session, url: str, timeout: float) -> FetchResult:
    try:
        resp = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        content_type = (resp.headers.get("content-type") or "").lower()
        if resp.status_code >= 400:
            return FetchResult(url=strip_sensitive_query_params(resp.url), text="", ok=False,
                               error=f"http_{resp.status_code}")
        if "html" not in content_type and content_type:
            return FetchResult(url=strip_sensitive_query_params(resp.url), text="", ok=False,
                               error="non_html")
        return FetchResult(url=strip_sensitive_query_params(resp.url),
                           text=resp.text or "", ok=True)
    except requests.RequestException as exc:
        return FetchResult(url=url, text="", ok=False, error=exc.__class__.__name__)


def _pick_obvious_page(homepage_url: str, html: str) -> Optional[str]:
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    for href, text in parser.links:
        if not href:
            continue
        joined = urljoin(homepage_url, href)
        split = urlsplit(joined)
        if split.scheme not in ("http", "https"):
            continue
        haystack = f"{split.path} {split.query} {text}".lower()
        if any(word in haystack for word in OBVIOUS_PAGE_WORDS):
            return strip_sensitive_query_params(joined)
    return None


def _detect_outdated(text: str) -> Optional[bool]:
    if OUTDATED_TEXT_RE.search(text):
        return True
    years = []
    for raw in YEAR_RE.findall(text):
        try:
            years.append(int(raw))
        except ValueError:
            continue
    if not years:
        return None
    current_year = datetime.now(timezone.utc).year
    return max(years) <= current_year - 4


def _status_payload(
    *,
    checked: bool,
    detected: Iterable[str] = (),
    missing: Iterable[str] = (),
    unknown: Iterable[str] = (),
    pages_checked: Iterable[str] = (),
    error: str = "",
) -> Dict[str, object]:
    detected_list = sorted(set(detected))
    missing_list = sorted(set(missing) - set(detected_list))
    unknown_list = sorted(set(unknown) - set(detected_list) - set(missing_list))
    return {
        "checked": checked,
        "detected": detected_list,
        "missing": missing_list,
        "unknown": unknown_list,
        "pages_checked": list(pages_checked),
        "retrieved_at": utcnow_iso(),
        "error": error,
    }


def analyze_website(
    website: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = min(float(REQUEST_TIMEOUT), 5.0),
    cache: Optional[Cache] = None,
) -> Dict[str, object]:
    """Analyze one competitor website with a maximum of two HTML fetches.

    When ``cache`` is provided, the derived signal payload (not the page HTML)
    is cached by normalized URL for ``WEBSITE_ANALYSIS_TTL_SECONDS``. The
    cached result is treated like any other source: the ``retrieved_at``
    field reflects the original fetch time, which is what the report shows.
    """
    normalized = _normalize_url(website)
    if not normalized:
        return _status_payload(
            checked=True,
            detected=["no_website"],
            unknown=[s for s in SIGNALS if s != "no_website"],
        )

    if _is_non_public_test_domain(normalized):
        return _status_payload(
            checked=False,
            unknown=[s for s in SIGNALS if s != "no_website"],
            pages_checked=[],
            error="non_public_test_domain",
        )

    def _live_fetch() -> Dict[str, object]:
        return _analyze_live(normalized, session=session, timeout=timeout)

    if cache is not None:
        value, as_of = cached_call(
            cache,
            provider="website_analysis",
            method="analyze",
            params={"url": normalized},
            ttl_seconds=WEBSITE_ANALYSIS_TTL_SECONDS,
            live_call=_live_fetch,
        )
        if isinstance(value, dict):
            payload = dict(value)
            payload["retrieved_at"] = as_of
            return payload
        return value

    return _live_fetch()


def _analyze_live(
    normalized: str,
    *,
    session: Optional[requests.Session],
    timeout: float,
) -> Dict[str, object]:
    """Perform the actual HTTP fetches and signal extraction.

    Split out so ``analyze_website`` can decide whether to wrap this in the
    SQLite response cache. Only signals are returned; page HTML is never
    persisted.
    """
    owns_session = session is None
    session = session or requests.Session()
    pages: List[FetchResult] = []
    try:
        home = _fetch(session, normalized, timeout)
        pages.append(home)
        if home.ok:
            obvious = _pick_obvious_page(home.url, home.text)
            if obvious and obvious != home.url:
                pages.append(_fetch(session, obvious, timeout))
    finally:
        if owns_session:
            session.close()

    fetched = [p for p in pages if p.ok and p.text]
    pages_checked = [p.url for p in fetched]
    if not fetched:
        error = "; ".join(p.error for p in pages if p.error) or "fetch_failed"
        return _status_payload(
            checked=False,
            unknown=[s for s in SIGNALS if s != "no_website"],
            pages_checked=pages_checked,
            error=error,
        )

    combined = "\n".join(p.text for p in fetched)
    detected: List[str] = []
    missing: List[str] = []
    unknown: List[str] = []

    checks = {
        "online_booking": BOOKING_RE.search(combined),
        "class_schedule": SCHEDULE_RE.search(combined),
        "pricing": PRICING_RE.search(combined),
        "multilingual_support": MULTILINGUAL_RE.search(combined),
        "certification_keywords": CERT_RE.search(combined),
        "acls_pals_offered": ACLS_PALS_RE.search(combined),
        "group_corporate_offered": GROUP_CORPORATE_RE.search(combined),
        "weekend_classes_offered": WEEKEND_RE.search(combined),
    }
    for signal, match in checks.items():
        (detected if match else missing).append(signal)

    if CONTACT_RE.search(combined):
        missing.append("contact_friction")
    else:
        detected.append("contact_friction")

    outdated = _detect_outdated(combined)
    if outdated is True:
        detected.append("outdated_website")
    elif outdated is False:
        missing.append("outdated_website")
    else:
        unknown.append("outdated_website")

    unknown.append("no_website")

    return _status_payload(
        checked=True,
        detected=detected,
        missing=missing,
        unknown=unknown,
        pages_checked=pages_checked,
    )
