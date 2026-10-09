"""
Self-healing model roster: before each run, health-checks every candidate in
cfg.model_pool with a tiny request, then selects up to `target_count` healthy
models preferring one-per-provider coverage.

Free-tier OpenRouter model availability churns: a model can get transiently
rate-limited (HTTP 429 -- retried with backoff, see llm_retry.py) or
permanently retired/paywalled (HTTP 404 "unavailable for free" -- not
retried, since no amount of waiting fixes that). Both are treated as "not
healthy for this run" here; the caller decides what to do if too few models
survive (main.py aborts the run rather than forecast with <2 models).
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


def _provider_of(model: str) -> str:
    bare = model.split("/", 1)[-1] if model.startswith("openrouter/") else model
    return bare.split("/", 1)[0]


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
    (selected, all_results). `selected` is capped at `target_count`,
    preferring to cover distinct providers before repeating one, and
    preserving the pool's ordering as the tiebreak within each pass.
    """
    results = await asyncio.gather(*[_check_one(m) for m in model_pool])
    healthy = [r.model for r in results if r.healthy]

    selected: list[str] = []
    seen_providers: set[str] = set()
    for model in healthy:
        if len(selected) >= target_count:
            break
        provider = _provider_of(model)
        if provider not in seen_providers:
            selected.append(model)
            seen_providers.add(provider)
    for model in healthy:
        if len(selected) >= target_count:
            break
        if model not in selected:
            selected.append(model)

    return selected, results
