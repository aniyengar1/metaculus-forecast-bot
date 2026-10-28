"""
Shared retry-with-backoff for LLM calls, used by both the forecasting bot
(metaculus_bot.bot) and the model health-checker (metaculus_bot.model_health).
Split out to its own module so the health-checker doesn't have to import the
whole bot module just to reuse this.
"""
from __future__ import annotations

import asyncio
import logging

import litellm

from forecasting_tools import GeneralLlm

logger = logging.getLogger(__name__)

# 429 (rate limit), 503 (service unavailable), 500 (internal server error),
# and request timeouts -- all transient upstream-congestion errors where a
# short wait gives the next attempt an actual chance of succeeding, unlike
# GeneralLlm's own near-instant `allowed_tries` retries. 503/500/Timeout
# added 2026-10-27: an audit found Gemini missing from ~70% of eligible
# ensembles, and these three error types accounted for 100% of sampled
# incidents -- none retried before this, since only RateLimitError was
# covered. See docs/STAGE_B_PLAN.md "Ensemble composition audit + Gemini
# reliability plan".
_RETRYABLE_EXCEPTIONS = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.Timeout,
)


async def invoke_with_backoff(llm: GeneralLlm, prompt: str, max_tries: int = 3) -> str:
    """Retries on the transient errors in _RETRYABLE_EXCEPTIONS, with
    exponential backoff (2s, 4s, ...), before giving up. Permanent errors
    (404/model retired, auth, etc) are not retried -- they're re-raised
    immediately since another attempt can't fix them."""
    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return await llm.invoke(prompt)
        except _RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt == max_tries:
                break
            backoff = 2**attempt  # 4s, 8s for attempt 2, 3 (attempt 1 -> 2s)
            logger.warning(
                f"{type(e).__name__} on {llm.model} (attempt {attempt}/{max_tries}), "
                f"backing off {backoff}s"
            )
            await asyncio.sleep(backoff)
    assert last_exc is not None
    raise last_exc
