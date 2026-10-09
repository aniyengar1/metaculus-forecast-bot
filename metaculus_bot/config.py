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

# Free OpenRouter models spanning 3+ providers, verified via
# https://openrouter.ai/api/v1/models to support structured outputs/tool
# calling. Swap to paid frontier models once Metaculus LLM credits are
# confirmed (see docs/PHASE1_RESEARCH.md).
#
# `models` is the static fallback ensemble (used only if something
# constructs the bot without going through the health-check flow, e.g.
# tests). The real run path (main.py) always health-checks `model_pool` at
# startup and substitutes whichever 3 come back healthy -- see
# metaculus_bot/model_health.py. Free-tier model availability churns fast
# (two different models broke in two different ways within 48h on
# 2026-10-05/06: one rate-limited, one permanently retired/paywalled), which
# is exactly why the pool has 6 candidates across 6 providers instead of a
# hand-picked 3.
DEFAULT_MODELS = [
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/apodex/apodex-1.1-mini:free",
    "openrouter/qwen/qwen3.8-27b:free",
]
DEFAULT_MODEL_POOL = [
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/apodex/apodex-1.1-mini:free",
    "openrouter/qwen/qwen3.8-27b:free",  # retired/paywalled as of 2026-10-07; kept in case it returns
    "openrouter/google/gemma-4-26b-a4b-it:free",  # rate-limited 2026-10-05; 429s are transient, kept as candidate
    "openrouter/inclusionai/ling-3.0-flash-sante:free",
    "openrouter/dots-studio/dots-3-note-preview:free",
]
DEFAULT_PARSER_MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"


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

    # OpenRouter free-tier rate limit is account-wide across ALL ":free"
    # models combined: 20 requests/minute always, and either 50/day (no
    # lifetime credits purchased) or 1000/day (>=$10 purchased once -- a
    # one-time real-money account upgrade, unrelated to Metaculus's
    # sponsored *model* credits). Deterministic regex parsing
    # (metaculus_bot/deterministic_parse.py) handles ~87% of model outputs
    # with zero extra requests, falling back to an LLM parser call only when
    # it can't; measured average is ~4.4 requests/question (1
    # research-summary + 3 forecast calls, occasionally +1 fallback parse),
    # plus ~6-10 for a model health-check pass (once per run that has new
    # questions). Default of 8 is sized for the 50/day tier (~5/question
    # planning number w/ ~20% margin, minus a 10-request health-check
    # reserve); see docs/STAGE_B_PLAN.md for the full math. Raise this (e.g.
    # to ~70) once $10 of OpenRouter credit has been purchased.
    max_questions_per_day: int = 8

    # Pre-flight OpenRouter quota guard for local/manual runs only (the
    # actual scheduled cron run is exempt -- see main.py's IS_SCHEDULED_RUN).
    # Reserve = 1 health-check pass (~10) + 1 question (~5) so the *next*
    # scheduled run still has a realistic chance to do something.
    min_quota_reserve_for_scheduled_run: int = 15
    enforce_rate_limit_guard: bool = True

    # Seasonal tournament (fall-futureeval-2026) stays off until paid LLM
    # credits + stronger models are confirmed -- weak free-tier forecasts
    # there would hurt the seasonal score. MiniBench runs regardless.
    enable_seasonal_tournament: bool = False

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
        max_questions_per_day=_get_int("MAX_QUESTIONS_PER_DAY", 8),
        min_quota_reserve_for_scheduled_run=_get_int("MIN_QUOTA_RESERVE_FOR_SCHEDULED_RUN", 15),
        enforce_rate_limit_guard=_get_bool("ENFORCE_RATE_LIMIT_GUARD", True),
        enable_seasonal_tournament=_get_bool("ENABLE_SEASONAL_TOURNAMENT", False),
        calibration_k=_get_float("CALIBRATION_K", 1.0),
        calibration_clip_min=_get_float("CALIBRATION_CLIP_MIN", 0.02),
        calibration_clip_max=_get_float("CALIBRATION_CLIP_MAX", 0.98),
        db_path=os.getenv("BOT_DB_PATH", "data/forecasts.db"),
    )


def asknews_creds_present() -> bool:
    has_oauth = bool(os.getenv("ASKNEWS_CLIENT_ID")) and bool(os.getenv("ASKNEWS_SECRET"))
    has_key = bool(os.getenv("ASKNEWS_API_KEY"))
    return has_oauth or has_key
