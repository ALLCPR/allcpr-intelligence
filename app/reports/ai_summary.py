"""
Optional AI executive summary (OpenAI / Groq).

This is the ONLY place the pipeline talks to a generative LLM, and it is
strictly opt-in: when no provider key is configured the functions here no-op
and the report is rendered exactly as before.

Design guarantees (consistent with the rest of the system):
  - **Never invents data.** The model is handed the already-computed
    deterministic interpretation (executive verdict, decision matrix, course
    performance) and is instructed to *rephrase only*, never to introduce a
    figure that is not in the provided context. The prompt says so explicitly,
    and the section is labeled "AI-generated narrative".
  - **Provider-agnostic.** OpenAI and Groq both speak the OpenAI
    ``/chat/completions`` API, so a single ``requests`` call serves either —
    no SDK dependency. Groq is a free, OpenAI-compatible option
    (console.groq.com); it is preferred by auto-detection.
  - **Fail-soft.** Any network/auth/quota error returns ``None`` and the report
    simply omits the AI section.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests

from app import config
from app.utils.logging_utils import get_logger

logger = get_logger(__name__)

# provider key -> (base_url, env attr for key, env attr for model)
_PROVIDERS: Dict[str, Dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_attr": "OPENAI_API_KEY",
        "model_attr": "OPENAI_MODEL",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_attr": "GROQ_API_KEY",
        "model_attr": "GROQ_MODEL",
    },
}


def resolve_provider() -> Optional[str]:
    """Return the active provider key ('openai'|'groq') or None.

    Honors an explicit ``LLM_PROVIDER`` when its key is present; otherwise
    auto-detects, preferring Groq (free) over OpenAI.
    """
    explicit = config.LLM_PROVIDER
    if explicit in _PROVIDERS and getattr(config, _PROVIDERS[explicit]["key_attr"], ""):
        return explicit
    # Unset, unknown, or explicit-but-keyless: auto-detect, preferring Groq.
    if config.GROQ_API_KEY:
        return "groq"
    if config.OPENAI_API_KEY:
        return "openai"
    return None


def is_configured() -> bool:
    return resolve_provider() is not None


# --------------------------------------------------------------------------- #
# Prompt assembly — only deterministic, already-computed figures go in.
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You are an assistant that writes the executive summary for a CPR/BLS "
    "training-center site-intelligence report. You are given a deterministic, "
    "already-computed analysis as JSON. Write a concise, decision-ready "
    "summary (120-200 words) for a business owner deciding where to open or "
    "what to schedule. STRICT RULES: use ONLY figures and facts present in the "
    "provided JSON; never invent numbers, scores, place names, prices, or "
    "enrollment; if something is unknown, say it is unknown; do not give legal "
    "or financial guarantees. Plain American English, no markdown headers."
)


def _compact_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pull just the decision-relevant, already-computed bits from the report."""
    context = payload.get("context") or {}
    report_interp = payload.get("report_interpretation") or {}
    ev = report_interp.get("executive_verdict") or {}
    matrix = report_interp.get("decision_matrix") or []
    course = context.get("course_performance") or {}

    course_brief: Dict[str, Any] = {}
    if course:
        course_brief = {
            "area_label": course.get("area_label"),
            "strategy": course.get("strategy"),
            "scheduling_recommendations": course.get("scheduling_recommendations"),
            "public_demand_vs_actual": (course.get("public_demand_vs_actual") or {}).get("notes"),
            "top_course_types": [
                {
                    "label": c.get("label"),
                    "average_students_per_class": c.get("average_students_per_class"),
                    "fill_rate_percent": c.get("fill_rate_percent"),
                    "course_performance_score": c.get("course_performance_score"),
                    "performance_band": c.get("performance_band"),
                }
                for c in (course.get("course_types") or [])[:8]
            ],
        }

    return {
        "mode": context.get("mode"),
        "cities": context.get("cities"),
        "executive_verdict": ev,
        "decision_matrix": matrix[:8],
        "course_performance": course_brief,
    }


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #

def _chat(provider: str, messages: List[Dict[str, str]]) -> Optional[str]:
    spec = _PROVIDERS[provider]
    api_key = getattr(config, spec["key_attr"], "")
    model = getattr(config, spec["model_attr"], "")
    if not api_key:
        return None
    url = f"{spec['base_url']}/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 400,
            }),
            timeout=config.LLM_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning(f"ai_summary: {provider} request failed: {exc}")
        return None
    if resp.status_code != 200:
        logger.warning(
            f"ai_summary: {provider} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(f"ai_summary: {provider} malformed response: {exc}")
        return None
    return (content or "").strip() or None


def generate_executive_summary(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate an AI executive summary, or None when not configured/failed.

    Returns ``{"provider", "model", "text"}`` so the report can attribute the
    narrative. Never raises — any failure degrades to ``None``.
    """
    provider = resolve_provider()
    if provider is None:
        return None
    ctx = _compact_context(payload)
    if not (ctx.get("executive_verdict") or ctx.get("course_performance")):
        return None  # nothing to summarize
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content":
            "Here is the deterministic site-intelligence analysis as JSON. "
            "Write the executive summary following the rules.\n\n"
            + json.dumps(ctx, default=str)[:12000]},
    ]
    text = _chat(provider, messages)
    if not text:
        return None
    model = getattr(config, _PROVIDERS[provider]["model_attr"], "")
    logger.info(f"ai_summary: generated via {provider}/{model} ({len(text)} chars)")
    return {"provider": provider, "model": model, "text": text}
