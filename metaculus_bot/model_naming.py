"""
Shared helpers for parsing our litellm-style model strings
("<provider>/<bare model id>", e.g. "groq/openai/gpt-oss-120b" or
"openrouter/apodex/apodex-1.1-mini:free"). Used by model_health.py (for
logging) and provider_quota.py (to route quota lookups to the right
provider).
"""
from __future__ import annotations


def provider_of(model: str) -> str:
    """"groq/openai/gpt-oss-120b" -> "groq"; "gemini/gemini-3.8-flash" ->
    "gemini"; "openrouter/apodex/apodex-1.1-mini:free" -> "openrouter"."""
    return model.split("/", 1)[0]


def bare_model_name(model: str) -> str:
    """Strips the leading provider segment: "groq/openai/gpt-oss-120b" ->
    "openai/gpt-oss-120b"."""
    return model.split("/", 1)[-1]
