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


async def invoke_with_backoff(llm: GeneralLlm, prompt: str, max_tries: int = 3) -> str:
    """Retries only on HTTP 429 (rate limit), with exponential backoff
    (2s, 4s, ...), before giving up. GeneralLlm's own `allowed_tries` already
    retries generic failures near-instantly, which does nothing for a 429
    caused by upstream shared free-pool congestion -- that needs an actual
    delay before the next attempt has a chance of succeeding. Permanent
    errors (404/model retired, auth, etc) are not retried -- they're
    re-raised immediately since another attempt can't fix them."""
    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return await llm.invoke(prompt)
        except litellm.RateLimitError as e:
            last_exc = e
            if attempt == max_tries:
                break
            backoff = 2**attempt  # 4s, 8s for attempt 2, 3 (attempt 1 -> 2s)
            logger.warning(
                f"Rate limited on {llm.model} (attempt {attempt}/{max_tries}), "
                f"backing off {backoff}s"
            )
            await asyncio.sleep(backoff)
    assert last_exc is not None
    raise last_exc
