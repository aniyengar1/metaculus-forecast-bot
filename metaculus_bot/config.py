"""
Central config for the FutureEval bot, read entirely from environment variables
(populated via .env locally, or GitHub Secrets/Variables in CI).

No secret values are ever read here beyond what dotenv/os.environ already holds;
this module only turns strings into typed settings. See .env.template for the
full list of variables and their defaults/documentation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Free-tier models across 3 real providers (Gemini, Groq, OpenRouter),
# verified working on 2026-10-12 (see docs/STAGE_B_PLAN.md "Multi-provider
# pool"). Priority order matters: gemini-3.8-flash and the two Groq models
# are the primary picks (each a genuinely different company/API, not just a
# different model on the same shared OpenRouter free pool), with the
# OpenRouter :free pool kept purely as a fallback tier if gemini/groq are
# both unhealthy. `model_health.select_healthy_models` now walks this list
# in order and takes the first `target_ensemble_size` healthy entries --
# no provider-diversity dedup, since the ordering already encodes that.
#
# `models` is the static fallback ensemble (used only if something
# constructs the bot without going through the health-check flow, e.g.
# tests). The real run path (main.py) always health-checks `model_pool` at
# startup and substitutes whichever N come back healthy.
DEFAULT_MODELS = [
    "gemini/gemini-3.8-flash",
    "groq/openai/gpt-oss-120b",
    "groq/qwen/qwen3.8-27b",
]
DEFAULT_MODEL_POOL = [
    "gemini/gemini-3.8-flash",
    "groq/openai/gpt-oss-120b",
    "groq/qwen/qwen3.8-27b",
    # OpenRouter :free fallback tier -- only reached if gemini/groq are both
    # unhealthy. Free-tier availability churns fast there (two different
    # models broke in two different ways within 48h on 2026-10-05/06: one
    # rate-limited, one permanently retired/paywalled), hence several
    # candidates rather than one.
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/apodex/apodex-1.1-mini:free",
    "openrouter/inclusionai/ling-3.0-flash-sante:free",
    "openrouter/dots-studio/dots-3-note-preview:free",
]
# Lighter-weight models for the parser/summarizer role (structured-output
# parsing fallback + research summarization) -- doesn't need frontier
# reasoning, just reliable instruction-following, so default to the
# cheapest-quota tier rather than reusing a primary ensemble slot.
DEFAULT_PARSER_MODEL_POOL = [
    "gemini/gemini-3.5-flash-lite",
    "groq/openai/gpt-oss-20b",
]
DEFAULT_PARSER_MODEL = "gemini/gemini-3.5-flash-lite"


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if val is None or not val.strip():
        return list(default)
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    # Ensemble
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    model_pool: list[str] = field(default_factory=lambda: list(DEFAULT_MODEL_POOL))
    parser_model_pool: list[str] = field(default_factory=lambda: list(DEFAULT_PARSER_MODEL_POOL))
    target_ensemble_size: int = 3
    n_runs: int = 3
    parser_model: str = DEFAULT_PARSER_MODEL

    # Budget guard
    max_cost_per_question: float = 0.40
    max_cost_per_day: float = 10.0
    budget_safety_margin: float = 1.3
    est_input_tokens_per_call: int = 3000
    est_output_tokens_per_call: int = 1500
    # Floor used only for models litellm has no pricing data for AND that are
    # not explicitly free (":free" suffix always estimates $0, since
    # OpenRouter itself guarantees free tier calls cost nothing). This stays
    # conservative so an unrecognized *paid* model doesn't silently estimate
    # as free.
    unknown_model_input_cost_per_token: float = 0.000003
    unknown_model_output_cost_per_token: float = 0.000015

    # AskNews
    asknews_max_calls_per_question: int = 2
    asknews_monthly_cap: int = 1000

    # As of 2026-10-12 the real daily throttle is computed dynamically each
    # run from actual remaining per-provider quota (metaculus_bot.provider_quota
    # .compute_daily_question_budget, with a 20% margin) across whichever
    # models are actually selected that run. This field is now just a hard
    # safety ceiling the computed value can never exceed, in case the quota
    # math is ever wrong in a way that would ask for too many questions --
    # it does not drive normal-day behavior anymore. See docs/STAGE_B_PLAN.md.
    max_questions_per_day_ceiling: int = 20

    # Pre-flight OpenRouter quota guard for local (non-CI) runs only --
    # any GitHub Actions run (scheduled or workflow_dispatch) is exempt,
    # see main.py's IS_CI_RUN. Reserve = 1 health-check pass (~10) + 1
    # question (~5) so the *next* scheduled run still has a realistic
    # chance to do something.
    min_quota_reserve_for_scheduled_run: int = 15
    enforce_rate_limit_guard: bool = True

    # Seasonal tournament (fall-futureeval-2026). Enabled 2026-10-12, on its
    # own 15-min schedule (run_bot_on_seasonal.yaml, offset from MiniBench's
    # so they interleave rather than collide on every trigger) -- all 26 real
    # questions pulled from the API on 2026-10-12 turned out to have the same
    # exact 3-hour open window as MiniBench, not the "days/weeks" originally
    # assumed; see docs/STAGE_B_PLAN.md "Seasonal cadence correction". Both
    # workflows share one concurrency group so they never race on the data
    # branch.
    enable_seasonal_tournament: bool = True
    # When computing the seasonal run's daily question budget, subtract this
    # many questions' worth of quota as headroom deliberately left for
    # MiniBench -- a user-specified priority (MiniBench wins when the two
    # compete for the same Groq/Gemini/OpenRouter quota), not a cadence
    # argument now that both poll every ~15 min.
    minibench_quota_reserve: int = 5

    # Seasonal re-forecasting: if a seasonal question we already forecast is
    # still open and our last forecast is older than this many days,
    # re-forecast it (new information may have changed things), capped at
    # this many re-forecasts per day so it can't crowd out fresh questions.
    # In practice this rarely/never fires given the 3h windows above (a
    # question is closed long before it could turn 7-days-stale while still
    # open) -- kept anyway since it costs nothing when it doesn't apply (a
    # local DB query against already-fetched questions, no extra Metaculus
    # API calls and zero LLM calls unless a genuine candidate exists) and
    # protects against the tournament's format changing later in the season.
    reforecast_staleness_days: int = 7
    max_reforecasts_per_day: int = 5

    # Calibration (applied to final binary probability only)
    calibration_k: float = 1.0
    calibration_clip_min: float = 0.02
    calibration_clip_max: float = 0.98

    # Storage
    db_path: str = "data/forecasts.db"


def load_config() -> Config:
    return Config(
        models=_get_list("BOT_MODELS", DEFAULT_MODELS),
        model_pool=_get_list("BOT_MODEL_POOL", DEFAULT_MODEL_POOL),
        parser_model_pool=_get_list("BOT_PARSER_MODEL_POOL", DEFAULT_PARSER_MODEL_POOL),
        target_ensemble_size=_get_int("BOT_TARGET_ENSEMBLE_SIZE", 3),
        n_runs=_get_int("BOT_N_RUNS", 3),
        parser_model=os.getenv("BOT_PARSER_MODEL", DEFAULT_PARSER_MODEL),
        max_cost_per_question=_get_float("MAX_COST_PER_QUESTION", 0.40),
        max_cost_per_day=_get_float("MAX_COST_PER_DAY", 10.0),
        budget_safety_margin=_get_float("BUDGET_SAFETY_MARGIN", 1.3),
        est_input_tokens_per_call=_get_int("EST_INPUT_TOKENS_PER_CALL", 3000),
        est_output_tokens_per_call=_get_int("EST_OUTPUT_TOKENS_PER_CALL", 1500),
        unknown_model_input_cost_per_token=_get_float(
            "UNKNOWN_MODEL_INPUT_COST_PER_TOKEN", 0.000003
        ),
        unknown_model_output_cost_per_token=_get_float(
            "UNKNOWN_MODEL_OUTPUT_COST_PER_TOKEN", 0.000015
        ),
        asknews_max_calls_per_question=_get_int("ASKNEWS_MAX_CALLS_PER_QUESTION", 2),
        asknews_monthly_cap=_get_int("ASKNEWS_MONTHLY_CAP", 1000),
        max_questions_per_day_ceiling=_get_int("MAX_QUESTIONS_PER_DAY_CEILING", 20),
        min_quota_reserve_for_scheduled_run=_get_int("MIN_QUOTA_RESERVE_FOR_SCHEDULED_RUN", 15),
        enforce_rate_limit_guard=_get_bool("ENFORCE_RATE_LIMIT_GUARD", True),
        enable_seasonal_tournament=_get_bool("ENABLE_SEASONAL_TOURNAMENT", True),
        minibench_quota_reserve=_get_int("MINIBENCH_QUOTA_RESERVE", 5),
        reforecast_staleness_days=_get_int("REFORECAST_STALENESS_DAYS", 7),
        max_reforecasts_per_day=_get_int("MAX_REFORECASTS_PER_DAY", 5),
        calibration_k=_get_float("CALIBRATION_K", 1.0),
        calibration_clip_min=_get_float("CALIBRATION_CLIP_MIN", 0.02),
        calibration_clip_max=_get_float("CALIBRATION_CLIP_MAX", 0.98),
        db_path=os.getenv("BOT_DB_PATH", "data/forecasts.db"),
    )


def asknews_creds_present() -> bool:
    has_oauth = bool(os.getenv("ASKNEWS_CLIENT_ID")) and bool(os.getenv("ASKNEWS_SECRET"))
    has_key = bool(os.getenv("ASKNEWS_API_KEY"))
    return has_oauth or has_key
