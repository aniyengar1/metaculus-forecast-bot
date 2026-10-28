"""Regression test for a real incident (2026-10-28): gemini-3.8-flash was
measured timing out on ~90s health checks (also hit 503/429 errors) while
gemini-3.5-flash answered the same trivial prompt in 1.4-2.7s every time.
Fix: gemini-3.5-flash added to the pool ahead of both gemini-3.8-flash and
the OpenRouter tier, since it's the more reliable of the two Gemini models
by direct measurement -- see docs/STAGE_B_PLAN.md "Ensemble composition
audit + Gemini reliability plan"."""
from __future__ import annotations

from metaculus_bot.config import DEFAULT_MODEL_POOL
from metaculus_bot.model_naming import provider_of


def test_second_gemini_model_present_in_pool():
    assert "gemini/gemini-3.5-flash" in DEFAULT_MODEL_POOL


def test_more_reliable_gemini_model_ordered_first():
    assert DEFAULT_MODEL_POOL.index("gemini/gemini-3.5-flash") < DEFAULT_MODEL_POOL.index(
        "gemini/gemini-3.8-flash"
    )


def test_both_gemini_models_ordered_before_openrouter_tier():
    openrouter_indices = [i for i, m in enumerate(DEFAULT_MODEL_POOL) if provider_of(m) == "openrouter"]
    first_openrouter_index = min(openrouter_indices)
    assert DEFAULT_MODEL_POOL.index("gemini/gemini-3.5-flash") < first_openrouter_index
    assert DEFAULT_MODEL_POOL.index("gemini/gemini-3.8-flash") < first_openrouter_index


def test_second_gemini_model_is_non_openrouter_so_health_checked_eagerly():
    """Unlike the OpenRouter fallback tier (only checked if the primary tier
    falls short), gemini-3.5-flash is provider "gemini" -- select_healthy_models
    checks it alongside the other primaries on every run."""
    assert provider_of("gemini/gemini-3.5-flash") == "gemini"
