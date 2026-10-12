"""
Proves the $ budget guard never blocks a Groq/Gemini-only forecast, even
though litellm reports real (non-zero) published pricing for those model
families -- both keys are genuinely free-tier with no card on file, so real
spend is always $0 regardless of what litellm's estimate says. See
metaculus_bot/provider_quota.py (split_real_and_estimated_cost,
is_free_tier_provider) and docs/STAGE_B_PLAN.md.
"""
from __future__ import annotations

from dataclasses import replace

from metaculus_bot.budget import BudgetGuard, estimate_question_cost, price_per_token
from metaculus_bot.config import Config
from metaculus_bot.provider_quota import is_free_tier_provider, split_real_and_estimated_cost


def test_is_free_tier_provider_groq_and_gemini():
    assert is_free_tier_provider("groq/openai/gpt-oss-120b")
    assert is_free_tier_provider("gemini/gemini-3.8-flash")
    assert is_free_tier_provider("gemini/gemini-3.5-flash-lite")


def test_is_free_tier_provider_openrouter_free_suffix_only():
    assert is_free_tier_provider("openrouter/nvidia/nemotron-3-super-120b-a12b:free")
    assert not is_free_tier_provider("openrouter/openai/gpt-4o")  # no :free suffix -> paid


def test_is_free_tier_provider_unknown_provider_defaults_paid():
    assert not is_free_tier_provider("anthropic/claude-3-7-sonnet-latest")


def test_split_real_and_estimated_cost_all_free_zeros_real_cost():
    models_used = {"groq/openai/gpt-oss-120b", "groq/qwen/qwen3.8-27b", "gemini/gemini-3.5-flash-lite"}
    real, estimated = split_real_and_estimated_cost(models_used, litellm_estimate=0.0159)
    assert real == 0.0
    assert estimated == 0.0159  # litellm's number is kept for reference, just not used for the cap


def test_split_real_and_estimated_cost_mixed_with_paid_model_counts_real():
    models_used = {"groq/openai/gpt-oss-120b", "anthropic/claude-3-7-sonnet-latest"}
    real, estimated = split_real_and_estimated_cost(models_used, litellm_estimate=0.05)
    assert real == 0.05  # any non-free-tier model in the mix -> treat the whole estimate as real
    assert estimated == 0.05


def test_split_real_and_estimated_cost_empty_models_falls_back_to_real():
    # Defensive default: if we somehow don't know which models were used,
    # don't silently zero out a real cost.
    real, estimated = split_real_and_estimated_cost(set(), litellm_estimate=0.02)
    assert real == 0.02


def test_price_per_token_is_zero_for_groq_and_gemini():
    cfg = Config()
    assert price_per_token("groq/openai/gpt-oss-120b", cfg) == (0.0, 0.0)
    assert price_per_token("gemini/gemini-3.8-flash", cfg) == (0.0, 0.0)


def test_budget_guard_never_blocks_groq_gemini_only_forecast():
    """End-to-end: simulate main.py's actual flow -- a tiny $/day cap, a
    Groq+Gemini-only ensemble, litellm reporting a deceptively large
    per-question estimate (as it now does for these model families), and
    confirm the guard never trips across many questions."""
    cfg = replace(
        Config(),
        models=["groq/openai/gpt-oss-120b", "groq/qwen/qwen3.8-27b"],
        parser_model="gemini/gemini-3.5-flash-lite",
        max_cost_per_question=0.001,  # deliberately far below litellm's reported estimate
        max_cost_per_day=0.005,
    )
    guard = BudgetGuard(cfg, spent_today=0.0)

    # Pre-flight estimate (budget.py) should already be ~$0 for an all-free-tier ensemble.
    preflight_estimate = estimate_question_cost(cfg)
    assert preflight_estimate == 0.0
    ok, reason = guard.check(preflight_estimate)
    assert ok, f"Pre-flight guard unexpectedly blocked: {reason}"

    # Post-call: litellm reports a large non-zero estimate (its real
    # published pricing for these models), but every model used is
    # confirmed free-tier, so real_cost must still be 0 and the guard must
    # never trip, no matter how many questions we process.
    litellm_reported_estimate = 0.0159  # observed value from a real dry run, 2026-10-12
    models_used = {cfg.models[0], cfg.models[1], cfg.parser_model}

    for _ in range(1000):
        real_cost, _ = split_real_and_estimated_cost(models_used, litellm_reported_estimate)
        assert real_cost == 0.0
        guard.record_actual(real_cost)
        ok, reason = guard.check(estimate_question_cost(cfg))
        assert ok, f"Guard blocked a Groq/Gemini-only forecast after repeated use: {reason}"

    assert guard.spent_today == 0.0
