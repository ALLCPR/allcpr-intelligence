"""
Tests for the optional AI executive summary (OpenAI / Groq).

No network: the HTTP layer is monkeypatched. These verify provider resolution,
fail-soft behavior, the no-invented-data prompt wiring, and HTML rendering.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.reports import ai_summary  # noqa: E402
from app.reports.html_report import _ai_summary_section  # noqa: E402
from app.reports.markdown_report import _ai_summary_block  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_keys(monkeypatch):
    """Start every test from a no-provider baseline."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4o-mini")


_PAYLOAD = {
    "context": {"mode": "metro_comparison", "cities": ["San Jose, CA"]},
    "report_interpretation": {
        "executive_verdict": {
            "best_candidate": "San Jose comparison area",
            "verdict": "Mixed — needs more data",
            "confidence": "High (77/100)",
        },
        "decision_matrix": [{"candidate": "San Jose", "tier": "C"}],
    },
}


# --------------------------------------------------------------------------- #
# Provider resolution
# --------------------------------------------------------------------------- #

def test_resolve_none_without_keys():
    assert ai_summary.resolve_provider() is None
    assert ai_summary.is_configured() is False


def test_auto_prefers_groq(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk_x")
    assert ai_summary.resolve_provider() == "groq"


def test_auto_falls_back_to_openai(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk_x")
    assert ai_summary.resolve_provider() == "openai"


def test_explicit_provider_honored(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk_x")
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    assert ai_summary.resolve_provider() == "openai"


def test_explicit_provider_without_key_falls_through(monkeypatch):
    # LLM_PROVIDER=openai but no OpenAI key, Groq key present -> still resolves.
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    assert ai_summary.resolve_provider() == "groq"


# --------------------------------------------------------------------------- #
# generate_executive_summary
# --------------------------------------------------------------------------- #

def test_generate_noop_when_unconfigured():
    assert ai_summary.generate_executive_summary(_PAYLOAD) is None


def test_generate_happy_path(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    captured = {}

    def fake_chat(provider, messages):
        captured["provider"] = provider
        captured["messages"] = messages
        return "San Jose looks promising for BLS classes."

    monkeypatch.setattr(ai_summary, "_chat", fake_chat)
    out = ai_summary.generate_executive_summary(_PAYLOAD)
    assert out["provider"] == "groq"
    assert out["model"] == "llama-3.3-70b-versatile"
    assert "BLS" in out["text"]
    # The prompt must carry the deterministic verdict and forbid invention.
    assert "never invent" in captured["messages"][0]["content"].lower()
    assert "San Jose comparison area" in captured["messages"][1]["content"]


def test_generate_returns_none_on_empty_model_output(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(ai_summary, "_chat", lambda p, m: None)
    assert ai_summary.generate_executive_summary(_PAYLOAD) is None


def test_generate_none_when_nothing_to_summarize(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(ai_summary, "_chat", lambda p, m: "should not be called")
    assert ai_summary.generate_executive_summary({"context": {}}) is None


# --------------------------------------------------------------------------- #
# HTTP layer fail-soft (monkeypatch requests.post)
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_chat_parses_ok(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    payload = {"choices": [{"message": {"content": "  ok text  "}}]}
    monkeypatch.setattr(ai_summary.requests, "post",
                        lambda *a, **k: _Resp(200, payload))
    assert ai_summary._chat("groq", [{"role": "user", "content": "hi"}]) == "ok text"


def test_chat_handles_http_error(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")
    monkeypatch.setattr(ai_summary.requests, "post",
                        lambda *a, **k: _Resp(401, text="bad key"))
    assert ai_summary._chat("groq", [{"role": "user", "content": "hi"}]) is None


def test_chat_handles_network_exception(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_x")

    def boom(*a, **k):
        raise ai_summary.requests.RequestException("timeout")

    monkeypatch.setattr(ai_summary.requests, "post", boom)
    assert ai_summary._chat("groq", [{"role": "user", "content": "hi"}]) is None


def test_chat_no_key_returns_none(monkeypatch):
    # provider selected but its key empty -> no call.
    monkeypatch.setattr(ai_summary.requests, "post",
                        lambda *a, **k: _Resp(200, {}))
    assert ai_summary._chat("openai", [{"role": "user", "content": "hi"}]) is None


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

def test_html_section_renders_when_present():
    ctx = {"ai_summary": {"provider": "groq", "model": "llama-3.3-70b-versatile",
                          "text": "Line one.\n\nLine two."}}
    html = _ai_summary_section(ctx)
    assert "AI executive summary" in html
    assert "Line one." in html and "Line two." in html
    assert "groq" in html


def test_html_section_empty_when_absent():
    assert _ai_summary_section({}) == ""
    assert _ai_summary_section({"ai_summary": {"text": ""}}) == ""


# --------------------------------------------------------------------------- #
# Markdown rendering (parallel to HTML — same summary, same guarantees)
# --------------------------------------------------------------------------- #

def test_markdown_block_renders_when_present():
    block = _ai_summary_block(
        {"provider": "groq", "model": "llama-3.3-70b-versatile",
         "text": "Line one.\n\nLine two."}
    )
    md = "\n".join(block)
    assert "## AI executive summary" in md
    assert "Line one." in md and "Line two." in md
    # Attribution must name the provider/model and label it AI-generated, and
    # promise it introduces no new figures (the no-invented-data guarantee).
    assert "groq" in md and "llama-3.3-70b-versatile" in md
    assert "AI-generated" in md
    assert "no new figures" in md


def test_markdown_block_empty_when_absent():
    assert _ai_summary_block(None) == []
    assert _ai_summary_block({}) == []
    assert _ai_summary_block({"text": ""}) == []
