"""
Per-provider daily quota tracking, replacing the old single static
MAX_QUESTIONS_PER_DAY with a budget computed from actual remaining quota
across whichever providers are in play this run (OpenRouter, Groq, Gemini).

Each provider exposes quota differently:
- OpenRouter: GET /api/v1/key (openrouter_quota.py) -- real-time, free,
  account-wide across all :free models combined.
- Groq: no live-read endpoint, but published fixed per-model limits
  (console.groq.com/docs/rate-limits) that apply *per model independently*
  (unlike OpenRouter's pooled account-wide cap). We track our own usage
  against those static numbers.
- Gemini: no published numbers and no quota-remaining headers on success
  (confirmed by inspecting real response headers -- just
  x-gemini-service-tier, nothing quota-related). The only place a real
  number ever appears is inside a 429's QuotaFailure.violations[].quotaValue
  body. We track our own usage against a conservative guessed ceiling until
  a real 429 teaches us the actual one (see handle_possible_quota_error),
  then remember it in provider_learned_limits.

This is necessarily approximate (we assume we're the only consumer of each
key, and don't account for every possible call site), which is why
compute_daily_question_budget applies a 20% margin on top.
"""
from __future__ import annotations

import logging
import re
import sqlite3

from metaculus_bot import db
from metaculus_bot.model_naming import bare_model_name, provider_of
from metaculus_bot.openrouter_quota import get_free_model_daily_quota

logger = logging.getLogger(__name__)

# console.groq.com/docs/rate-limits, free tier, checked 2026-10-12. Each
# model has its own independent daily cap (not pooled like OpenRouter).
GROQ_FREE_TIER_RPD: dict[str, int] = {
    "openai/gpt-oss-120b": 1000,
    "openai/gpt-oss-20b": 1000,
    "qwen/qwen3.8-27b": 1000,
}
_GROQ_DEFAULT_RPD = 1000  # fallback for any Groq model not in the table above

# Gemini publishes no fixed free-tier numbers (varies by account, "check the
# AI Studio dashboard"). This is a conservative placeholder used only until
# handle_possible_quota_error() learns the real number from an actual 429.
_GEMINI_DEFAULT_RPD_GUESS = 1000

# Measured 2026-10-08/09 (see docs/STAGE_B_PLAN.md): ~4.4 total
# requests/question across a 3-model ensemble + 1 parser role. Each ensemble
# model gets exactly 1 forecast call/question; the remaining ~1.4 (1
# research-summary + occasional fallback parse) land on the parser model.
ENSEMBLE_CALLS_PER_QUESTION = 1.0
PARSER_CALLS_PER_QUESTION = 1.4

_QUOTA_VALUE_RE = re.compile(r'"quotaValue"\s*:\s*"?(\d+)"?')
_QUOTA_ID_RE = re.compile(r'"quotaId"\s*:\s*"([^"]*)"')


def record_usage(conn: sqlite3.Connection, model: str) -> None:
    """Call after every successful real LLM request (not health checks --
    those are a fixed, separate, small cost already accounted for in the
    budget's margin)."""
    provider = provider_of(model)
    if provider == "openrouter":
        return  # OpenRouter usage is read back live from their API instead
    db.increment_provider_usage(conn, provider, bare_model_name(model))


def handle_possible_quota_error(conn: sqlite3.Connection, model: str, exception: Exception) -> None:
    """Call from an except block when a real request fails. If this is a
    Gemini per-day quota violation, learn the real limit from it instead of
    continuing to guess."""
    provider = provider_of(model)
    if provider != "gemini":
        return
    text = str(exception)
    quota_id_match = _QUOTA_ID_RE.search(text)
    quota_value_match = _QUOTA_VALUE_RE.search(text)
    if not quota_value_match:
        return
    if quota_id_match and "PerDay" not in quota_id_match.group(1):
        return  # a per-minute violation, not the daily limit we care about
    try:
        rpd = int(quota_value_match.group(1))
    except ValueError:
        return
    logger.warning(f"Learned real Gemini daily quota for {model} from a 429: {rpd} requests/day")
    db.set_learned_rpd_limit(conn, "gemini", bare_model_name(model), rpd)


def get_remaining_quota(model: str, conn: sqlite3.Connection) -> int | None:
    """Returns None if remaining quota for this model's provider is
    unknowable (shouldn't normally happen for our 3 supported providers)."""
    provider = provider_of(model)
    bare = bare_model_name(model)

    if provider == "openrouter":
        quota = get_free_model_daily_quota()
        return quota.remaining if quota else None

    if provider == "groq":
        limit = GROQ_FREE_TIER_RPD.get(bare, _GROQ_DEFAULT_RPD)
        used = db.get_provider_usage_today(conn, "groq", bare)
        return max(limit - used, 0)

    if provider == "gemini":
        limit = db.get_learned_rpd_limit(conn, "gemini", bare) or _GEMINI_DEFAULT_RPD_GUESS
        used = db.get_provider_usage_today(conn, "gemini", bare)
        return max(limit - used, 0)

    return None


def compute_daily_question_budget(
    selected_models: list[str],
    parser_model: str,
    conn: sqlite3.Connection,
    ceiling: int,
    margin: float = 1.2,
) -> int:
    """The actual daily throttle: the tightest "how many more questions can
    this model's remaining quota support, given its role" across every model
    in play this run, divided by `margin` for safety, capped at `ceiling`."""
    per_model_question_budgets: list[float] = []

    for model in selected_models:
        remaining = get_remaining_quota(model, conn)
        if remaining is not None:
            per_model_question_budgets.append(remaining / ENSEMBLE_CALLS_PER_QUESTION)

    parser_remaining = get_remaining_quota(parser_model, conn)
    if parser_remaining is not None:
        per_model_question_budgets.append(parser_remaining / PARSER_CALLS_PER_QUESTION)

    if not per_model_question_budgets:
        logger.warning("Could not determine remaining quota for any model; using the hard ceiling.")
        return ceiling

    computed = int(min(per_model_question_budgets) / margin)
    return max(min(computed, ceiling), 0)
