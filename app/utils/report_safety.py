"""Safety helpers for persisted reports."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_KEYS = {
    "key", "api_key", "apikey", "token", "access_token", "signature",
    "client_secret", "secret",
}


def strip_sensitive_query_params(url: str) -> str:
    """Remove obvious API-key/token parameters from a URL before rendering."""
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        key = k.lower()
        if key in SENSITIVE_QUERY_KEYS:
            continue
        if "token" in key or "secret" in key or key.endswith("_key"):
            continue
        kept.append((k, v))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(kept, doseq=True),
        parts.fragment,
    ))


def contains_secret(text: str, secret: str) -> bool:
    """Tiny test helper: true only for non-empty secret literals."""
    return bool(secret) and secret in (text or "")
