"""
Self-healing model roster: before each run, health-checks candidates in
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

Two cost-control measures added 2026-10-19 after OpenRouter's entire
50/day account-wide free-tier cap got burned by health checks alone (0 real
forecast calls that day) within about 2 hours of the GCP VM backup poller
running every ~10 min -- every run was health-checking all 4 OpenRouter
fallback models even though Gemini/Groq (the primary tier) were healthy the
whole time and never actually needed the fallback:

1. The OpenRouter fallback tier (any model whose provider_of() returns
   "openrouter") is only health-checked at all if the primary
   (non-OpenRouter) models don't already satisfy target_count.
2. Every health check result (primary or fallback) is cached in SQLite
   (metaculus_bot.db.get_cached_model_health/set_cached_model_health) --
   a healthy verdict for 1 hour, an unhealthy one for only 5 minutes (see
   _HEALTH_CACHE_NEGATIVE_TTL_SECONDS below) -- so a model doesn't get
   re-pinged needlessly often when healthy, but a transient blip doesn't
   lock it out of the ensemble for a full hour either.

See docs/STAGE_B_PLAN.md "OpenRouter health-check quota drain" and
"Ensemble composition audit + Gemini reliability plan" for the measured
before/after numbers.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from forecasting_tools import GeneralLlm

from metaculus_bot import db
from metaculus_bot.llm_retry import invoke_with_backoff
from metaculus_bot.model_naming import provider_of

logger = logging.getLogger(__name__)

_HEALTH_CHECK_PROMPT = "Reply with exactly one word: OK"
# Kept at 20s (considered raising to 60s on 2026-10-28, reverted): measuring
# gemini-3.8-flash directly showed successful completions anywhere from
# ~11s up to a full 90s timeout, but also 3 separate 90s hangs with zero
# response and 503/429 errors -- a longer per-attempt timeout mostly just
# makes each of the 3 retries (_HEALTH_CHECK_MAX_TRIES) individually slower
# without reliably rescuing those hangs, so a model this unreliable is
# better served by gemini-3.5-flash now sitting ahead of it in
# DEFAULT_MODEL_POOL (see docs/STAGE_B_PLAN.md "Ensemble composition audit
# + Gemini reliability plan") than by a longer timeout here.
_HEALTH_CHECK_TIMEOUT_SECONDS = 20
_HEALTH_CHECK_MAX_TRIES = 3  # reuses the same backoff as real forecast calls
_HEALTH_CACHE_TTL_SECONDS = 3600
# Deliberately much shorter than the healthy TTL: a 2026-10-27 audit found
# Gemini missing from ~70% of eligible ensembles, almost entirely
# single-timestamp 503/500/Timeout blips rather than sustained outages (see
# docs/STAGE_B_PLAN.md "Ensemble composition audit + Gemini reliability
# plan") -- caching "dead" for the same 1h as "healthy" needlessly keeps a
# model out of every run in that window even after it's recovered.
_HEALTH_CACHE_NEGATIVE_TTL_SECONDS = 300


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


async def _check_models_cached(conn: sqlite3.Connection, models: list[str]) -> dict[str, HealthResult]:
    """Checks only the models not already cached (<=1h old); reuses the
    cached verdict for everything else -- no network call at all for a
    model checked within the last hour."""
    results: dict[str, HealthResult] = {}
    to_check: list[str] = []
    for model in models:
        cached = db.get_cached_model_health(
            conn,
            model,
            healthy_ttl_seconds=_HEALTH_CACHE_TTL_SECONDS,
            unhealthy_ttl_seconds=_HEALTH_CACHE_NEGATIVE_TTL_SECONDS,
        )
        if cached is not None:
            healthy, error = cached
            results[model] = HealthResult(model=model, healthy=healthy, error=error)
        else:
            to_check.append(model)

    if to_check:
        fresh = await asyncio.gather(*[_check_one(m) for m in to_check])
        for r in fresh:
            results[r.model] = r
            db.set_cached_model_health(conn, r.model, r.healthy, r.error)

    return results


async def select_healthy_models(
    conn: sqlite3.Connection, model_pool: list[str], target_count: int = 3
) -> tuple[list[str], list[HealthResult]]:
    """
    Health-checks `model_pool` (cached results preferred -- see
    _check_models_cached), then returns (selected, all_results). `selected`
    is the first `target_count` healthy models in pool order. Only checks
    the OpenRouter fallback tier if the primary models don't already
    satisfy `target_count` -- see module docstring.
    """
    primary = [m for m in model_pool if provider_of(m) != "openrouter"]
    fallback = [m for m in model_pool if provider_of(m) == "openrouter"]

    results = await _check_models_cached(conn, primary)
    healthy_primary = [m for m in primary if results[m].healthy]

    if len(healthy_primary) < target_count and fallback:
        results.update(await _check_models_cached(conn, fallback))

    selected = [m for m in model_pool if results.get(m) and results[m].healthy][:target_count]
    all_results = [results[m] for m in model_pool if m in results]
    return selected, all_results
