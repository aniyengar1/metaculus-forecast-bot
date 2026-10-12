"""
Self-healing model roster: before each run, health-checks every candidate in
cfg.model_pool with a tiny request, then selects the first `target_count`
healthy models in pool order.

Free-tier model availability churns: a model can get transiently
rate-limited (HTTP 429 -- retried with backoff, see llm_retry.py) or
permanently retired/paywalled (HTTP 404 "unavailable for free" -- not
retried, since no amount of waiting fixes that). Both are treated as "not
healthy for this run" here; the caller decides what to do if too few models
survive (main.py aborts the run rather than forecast with <2 models).

Selection is pure priority order, not provider-diversity-dedup: as of
2026-10-12 the pool is explicitly hand-ordered (gemini, then 2 distinct Groq
models, then an OpenRouter :free fallback tier), so preferring one-per-
provider would actually fight the intended ordering -- it would skip the
second (intentional) Groq model in favor of jumping straight to the
OpenRouter fallback tier. See docs/STAGE_B_PLAN.md "Multi-provider pool".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from forecasting_tools import GeneralLlm

from metaculus_bot.llm_retry import invoke_with_backoff

logger = logging.getLogger(__name__)

_HEALTH_CHECK_PROMPT = "Reply with exactly one word: OK"
_HEALTH_CHECK_TIMEOUT_SECONDS = 20
_HEALTH_CHECK_MAX_TRIES = 3  # reuses the same backoff as real forecast calls


@dataclass
class HealthResult:
    model: str
    healthy: bool
    error: str | None = None


async def _check_one(model: str) -> HealthResult:
    try:
        llm = GeneralLlm(
            model=model, temperature=0.0, allowed_tries=1, timeout=_HEALTH_CHECK_TIMEOUT_SECONDS
        )
        await invoke_with_backoff(llm, _HEALTH_CHECK_PROMPT, max_tries=_HEALTH_CHECK_MAX_TRIES)
        return HealthResult(model=model, healthy=True)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.warning(f"Health check failed for {model}: {error}")
        return HealthResult(model=model, healthy=False, error=error)


async def select_healthy_models(
    model_pool: list[str], target_count: int = 3
) -> tuple[list[str], list[HealthResult]]:
    """
    Health-checks every model in `model_pool` concurrently, then returns
    (selected, all_results). `selected` is the first `target_count` healthy
    models in pool order.
    """
    results = await asyncio.gather(*[_check_one(m) for m in model_pool])
    results_by_model = {r.model: r for r in results}
    selected = [m for m in model_pool if results_by_model[m].healthy][:target_count]
    return selected, results
