"""
Hard budget guard: estimates $ cost for a question *before* spending anything,
and skips (never calls a model) if the question would push spend over either
the per-question or per-day cap. Actual spend is tracked separately via
forecasting_tools' own MonetaryCostManager (which reads real cost from
litellm/OpenRouter after each call) -- record_actual() folds that real number
back into the running daily total so later questions in the same run see it.

Pricing lookup:
- Models whose name ends in ":free" are hard-coded to $0/token, since
  OpenRouter guarantees those calls are free regardless of what litellm's
  static pricing table knows.
- Known paid models use litellm.get_model_info() for real per-token pricing.
- Unknown paid models (not in litellm's table) fall back to a conservative
  non-zero floor (config: UNKNOWN_MODEL_*_COST_PER_TOKEN) rather than $0, so
  switching to an unrecognized paid model can't silently bypass the guard.
"""
from __future__ import annotations

import logging

from metaculus_bot.config import Config

logger = logging.getLogger(__name__)


def price_per_token(model: str, cfg: Config) -> tuple[float, float]:
    """Returns (input_cost_per_token, output_cost_per_token)."""
    bare_model = model.split("/", 1)[-1] if model.startswith("openrouter/") else model
    if model.endswith(":free") or bare_model.endswith(":free"):
        return 0.0, 0.0

    try:
        import litellm

        info = litellm.get_model_info(model)
        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        if input_cost is not None and output_cost is not None:
            return float(input_cost), float(output_cost)
    except Exception as e:
        logger.debug(f"No litellm pricing info for model {model}: {e}")

    return cfg.unknown_model_input_cost_per_token, cfg.unknown_model_output_cost_per_token


def estimate_call_cost(model: str, cfg: Config) -> float:
    input_cost, output_cost = price_per_token(model, cfg)
    return (
        cfg.est_input_tokens_per_call * input_cost
        + cfg.est_output_tokens_per_call * output_cost
    )


def estimate_question_cost(cfg: Config) -> float:
    """
    Call plan per question: one forecast call per ensemble run, one parser
    call per ensemble run (structure_output), one research-summarization
    call. Multiplied by a safety margin since this is a rough estimate (real
    token counts vary with research length, retries, etc).
    """
    forecast_calls_cost = sum(
        estimate_call_cost(cfg.models[i % len(cfg.models)], cfg)
        for i in range(cfg.n_runs)
    )
    parser_calls_cost = cfg.n_runs * estimate_call_cost(cfg.parser_model, cfg)
    research_summary_cost = estimate_call_cost(cfg.parser_model, cfg)
    total = forecast_calls_cost + parser_calls_cost + research_summary_cost
    return total * cfg.budget_safety_margin


class BudgetGuard:
    def __init__(self, cfg: Config, spent_today: float) -> None:
        self.cfg = cfg
        self._spent_today = spent_today

    @property
    def spent_today(self) -> float:
        return self._spent_today

    def check(self, estimated_cost: float) -> tuple[bool, str | None]:
        if estimated_cost > self.cfg.max_cost_per_question:
            return False, (
                f"Estimated cost ${estimated_cost:.4f} exceeds per-question cap "
                f"${self.cfg.max_cost_per_question:.4f}"
            )
        if self._spent_today + estimated_cost > self.cfg.max_cost_per_day:
            return False, (
                f"Would push today's spend to ${self._spent_today + estimated_cost:.4f}, "
                f"exceeding daily cap ${self.cfg.max_cost_per_day:.4f}"
            )
        return True, None

    def record_actual(self, actual_cost: float) -> None:
        self._spent_today += max(actual_cost, 0.0)
