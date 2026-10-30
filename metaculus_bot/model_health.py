"""
Self-healing model roster: before each run, health-checks candidates with a
tiny request, then selects which models actually make the ensemble.

Free-tier model availability churns: a model can get transiently
rate-limited (HTTP 429 -- retried with backoff, see llm_retry.py) or
permanently retired/paywalled (HTTP 404 "unavailable for free" -- not
retried, since no amount of waiting fixes that). Both are treated as "not
healthy for this run" here; the caller decides what to do if too few models
survive (main.py aborts the run rather than forecast with <2 models).

Two selection functions, for two different shapes of candidate list:

- select_ensemble_by_slot: the primary ensemble (cfg.model_slots). Each
  "slot" (Gemini, gpt-oss-120b, qwen3.8-27b) gets exactly one model --
  see its own docstring for why this replaced a flat priority list
  2026-10-30.
- select_healthy_models: everything else (currently just the parser/
  summarizer pool, cfg.parser_model_pool) -- a flat priority-ordered list,
  "first target_count healthy entries in list order." As of 2026-10-12
  that pool is explicitly hand-ordered (gemini, then Groq, then an
  OpenRouter :free fallback tier), so preferring one-per-provider would
  actually fight the intended ordering. See docs/STAGE_B_PLAN.md
  "Multi-provider pool".

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
    satisfy `target_count` -- see module docstring. Generic flat-list
    selection, still used for the parser/summarizer pool (main.py); the
    primary ensemble uses select_ensemble_by_slot instead (below).
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


async def select_ensemble_by_slot(
    conn: sqlite3.Connection, slots: list[list[str]], openrouter_fallback_pool: list[str]
) -> tuple[list[str], list[HealthResult]]:
    """
    Selects exactly one model per slot (ensemble size == len(slots)), fixing
    a 2026-10-30 bug in the old flat-priority-list `select_healthy_models`:
    with gemini-3.5-flash and gemini-3.8-flash both early in that list, a
    fully healthy run selected both of them and dropped qwen3.8-27b
    entirely -- "first N healthy in list order" doesn't preserve the
    intended one-model-per-provider-slot diversity once a slot has more
    than one candidate.

    For each slot, checks its candidates (cached results preferred) and
    takes the first healthy one in the slot's own preference order. A slot
    left unfilled (every candidate unhealthy) is instead filled from
    `openrouter_fallback_pool` -- checked only if at least one slot needs
    it, same cost-control rationale as select_healthy_models' fallback
    tier. If the fallback pool doesn't have enough healthy models for
    every unfilled slot, the ensemble just comes back smaller than
    len(slots) (caller already handles <2 models as an abort condition).

    Returns (selected, all_results) -- selected has at most one model per
    slot; all_results covers every candidate actually checked, slot
    candidates first (in slot order), then any fallback candidates.
    """
    selected: list[str] = []
    all_results: list[HealthResult] = []
    unfilled_slots = 0

    for slot_candidates in slots:
        results = await _check_models_cached(conn, slot_candidates)
        all_results.extend(results[m] for m in slot_candidates)
        healthy_in_slot = [m for m in slot_candidates if results[m].healthy]
        if healthy_in_slot:
            selected.append(healthy_in_slot[0])
        else:
            unfilled_slots += 1

    if unfilled_slots and openrouter_fallback_pool:
        fallback_results = await _check_models_cached(conn, openrouter_fallback_pool)
        all_results.extend(fallback_results[m] for m in openrouter_fallback_pool)
        healthy_fallback = [m for m in openrouter_fallback_pool if fallback_results[m].healthy]
        selected.extend(healthy_fallback[:unfilled_slots])

    return selected, all_results
