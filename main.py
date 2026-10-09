import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

import dotenv

from bot_helpers import (
    check_environment,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
)

silence_noisy_dependencies()

from forecasting_tools import (  # noqa: E402
    BinaryQuestion,
    DateQuestion,
    DiscreteQuestion,
    GeneralLlm,
    MetaculusClient,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
)

from metaculus_bot import db  # noqa: E402
from metaculus_bot.bot import BudgetSkipped, FutureEvalBot  # noqa: E402
from metaculus_bot.budget import BudgetGuard  # noqa: E402
from metaculus_bot.config import load_config  # noqa: E402
from metaculus_bot.model_health import select_healthy_models  # noqa: E402
from metaculus_bot.openrouter_quota import get_free_model_daily_quota  # noqa: E402

dotenv.load_dotenv()
logger = logging.getLogger(__name__)

# Scope decision (Phase 2): only the Fall 2026 seasonal tournament + current
# MiniBench round. Metaculus Cup and Market Pulse are deliberately excluded
# for now -- Market Pulse needs continuous intra-day updating we haven't
# built, Cup is lower priority. Revisit once Stage A/B are solid.
TOURNAMENT_URLS = {
    "tournament": "https://www.metaculus.com/tournament/fall-futureeval-2026/",
    "test_questions": "https://www.metaculus.com/tournament/bot-testing-area/",
}

_TYPE_PRIORITY = [BinaryQuestion, MultipleChoiceQuestion, NumericQuestion, DateQuestion, DiscreteQuestion]

# GitHub Actions sets GITHUB_EVENT_NAME=schedule only for cron-triggered runs
# (not workflow_dispatch, and unset entirely when run locally). The rate
# limit guard below exempts exactly this case -- it exists to stop
# local/manual runs from starving the scheduled production run of shared
# account-wide OpenRouter quota, so the production run itself must never be
# the thing it blocks.
IS_SCHEDULED_RUN = os.getenv("GITHUB_EVENT_NAME") == "schedule"


def _check_rate_limit_guard(cfg) -> bool:
    """Returns False if the run should abort. Never calls sys.exit itself --
    asyncio.run()/nest_asyncio don't propagate SystemExit out of a task
    cleanly (it surfaces as an ugly 'Task exception was never retrieved'
    instead of a clean exit), so the actual process exit happens once in
    __main__, after the event loop has finished."""
    if IS_SCHEDULED_RUN:
        return True

    quota = get_free_model_daily_quota()
    if quota is None:
        print("⚠️  Could not check OpenRouter free-model quota before starting; proceeding anyway.\n")
        return True

    print(
        f"OpenRouter free-model daily quota: {quota.used}/{quota.limit} used, "
        f"{quota.remaining} remaining.\n"
    )
    if quota.remaining < cfg.min_quota_reserve_for_scheduled_run:
        msg = (
            f"Only {quota.remaining} OpenRouter free-model request(s) left today (limit "
            f"{quota.limit}), below the {cfg.min_quota_reserve_for_scheduled_run} reserved for "
            "the next scheduled production run. This is a local/manual run, and local testing "
            "shares the same account-wide daily quota as the scheduled MiniBench run."
        )
        if cfg.enforce_rate_limit_guard:
            print(
                f"🚨 REFUSING TO START: {msg}\n"
                "Set ENFORCE_RATE_LIMIT_GUARD=false to override (not recommended -- this is "
                "exactly how production got starved before).\n"
            )
            return False
        else:
            print(f"⚠️  {msg}\nContinuing anyway because ENFORCE_RATE_LIMIT_GUARD=false.\n")
    return True


def _select_diverse_subset(
    questions: list[MetaculusQuestion], limit: int
) -> list[MetaculusQuestion]:
    """Greedily picks one question per type (in _TYPE_PRIORITY order) first,
    then fills remaining slots with whatever's left, up to `limit`."""
    if limit <= 0 or len(questions) <= limit:
        return questions

    remaining = list(questions)
    selected: list[MetaculusQuestion] = []
    for qtype in _TYPE_PRIORITY:
        if len(selected) >= limit:
            break
        for q in remaining:
            if isinstance(q, qtype):
                selected.append(q)
                remaining.remove(q)
                break
    for q in remaining:
        if len(selected) >= limit:
            break
        selected.append(q)
    return selected[:limit]


def _fetch_questions(
    client: MetaculusClient, mode: str, target: str, cfg
) -> list[MetaculusQuestion]:
    if mode == "test_questions":
        return client.get_all_open_questions_from_tournament("bot-testing-area")

    questions: list[MetaculusQuestion] = []
    if target in ("both", "seasonal"):
        if cfg.enable_seasonal_tournament:
            questions += client.get_all_open_questions_from_tournament(
                client.CURRENT_AI_COMPETITION_ID
            )
        else:
            print(
                "  (seasonal tournament skipped: ENABLE_SEASONAL_TOURNAMENT is False "
                "-- set it once paid credits + stronger models are confirmed)"
            )
    if target in ("both", "minibench"):
        questions += client.get_all_open_questions_from_tournament(
            client.CURRENT_MINIBENCH_ID
        )
    return questions


async def _run(args: argparse.Namespace) -> int:
    """Returns a process exit code (0 success, 1 aborted)."""
    cfg = load_config()
    check_environment(strict=True)
    print_startup_banner(args.mode, will_publish=args.publish)
    if not _check_rate_limit_guard(cfg):
        return 1

    with db.connect(cfg.db_path) as conn:
        # Fetch and narrow down the question list *before* touching
        # OpenRouter at all (health check included) -- both the Metaculus
        # API and question filtering are free, and skipping the health
        # check when there's nothing to forecast saves ~6-10 OpenRouter
        # requests/run against the free tier's tight daily cap (see
        # docs/STAGE_B_PLAN.md).
        client = MetaculusClient()
        if args.post_ids:
            ids = [int(x.strip()) for x in args.post_ids.split(",") if x.strip()]
            questions = [client.get_question_by_post_id(pid) for pid in ids]
        else:
            questions = _fetch_questions(client, args.mode, args.target, cfg)

        # Log every open question we've ever seen (independent of whether we
        # forecast it *this* run) so we can tell, on a later run, whether one
        # closed while we were still deferring it -- see the alert check
        # right below.
        for q in questions:
            db.upsert_seen_question(
                conn,
                question_id=q.id_of_question,
                post_id=q.id_of_post,
                question_title=q.question_text,
                question_url=q.page_url,
                close_time_iso=q.close_time.isoformat() if q.close_time else None,
            )

        skip_previously_forecasted = args.mode != "test_questions"
        if skip_previously_forecasted:
            already_forecasted = [q for q in questions if q.already_forecasted]
            for q in already_forecasted:
                db.mark_question_forecasted(conn, q.id_of_question)
            questions = [q for q in questions if not q.already_forecasted]

        newly_closed = db.find_newly_closed_unforecast(conn)
        for question_id, title, url, close_time in newly_closed:
            msg = f"Question closed without ever being forecast: {url} ({title}) -- closed {close_time}"
            logger.warning(msg)
            db.log_alert(conn, msg)
            print(f"🚨 ALERT: {msg}")
            db.mark_closed_unforecast_alerted(conn, question_id)

        # Soonest-closing first, so a tight daily/--limit cap drops the
        # questions with the most runway left, not an arbitrary subset.
        # Questions with no close_time (shouldn't normally happen) sort last.
        _DISTANT_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
        questions.sort(key=lambda q: q.close_time or _DISTANT_FUTURE)

        if args.limit:
            questions = (
                _select_diverse_subset(questions, args.limit)
                if args.type_diverse
                else questions[: args.limit]
            )

        questions_today = db.get_questions_forecast_today(conn)
        remaining_quota = max(cfg.max_questions_per_day - questions_today, 0)
        if remaining_quota < len(questions):
            print(
                f"Throttling: {questions_today} question(s) already forecast today, "
                f"cap is {cfg.max_questions_per_day}/day -- processing the {remaining_quota} "
                f"soonest-closing of {len(questions)} found, rest deferred to a later run "
                "(not dropped -- will be retried, and alerted on if one closes first)."
            )
            questions = questions[:remaining_quota]

        if not questions:
            print("Nothing to forecast this run (no new questions, or daily cap reached).\n")
            return 0

        print(f"Health-checking model pool ({len(cfg.model_pool)} candidates)...")
        selected_models, health_results = await select_healthy_models(
            cfg.model_pool, target_count=cfg.target_ensemble_size
        )
        for r in health_results:
            status = "healthy" if r.healthy else f"DEAD -- {r.error}"
            print(f"  {r.model:<55} {status}")
        print()

        if len(selected_models) < 2:
            alert_msg = (
                f"Only {len(selected_models)} healthy model(s) out of pool {cfg.model_pool} "
                f"-- aborting run rather than forecasting with fewer than 2 models."
            )
            logger.error(alert_msg)
            db.log_alert(conn, alert_msg)
            print(f"🚨 ALERT: {alert_msg}\n")
            return 1

        effective_n_runs = (
            cfg.target_ensemble_size if len(selected_models) >= cfg.target_ensemble_size else len(selected_models)
        )
        cfg = replace(
            cfg,
            models=selected_models,
            n_runs=effective_n_runs,
            parser_model=selected_models[0],
        )

        spent_today = db.get_cost_spent_today(conn)
        budget_guard = BudgetGuard(cfg, spent_today)
        print(
            f"Budget: ${spent_today:.4f} spent today / ${cfg.max_cost_per_day:.2f} daily cap, "
            f"${cfg.max_cost_per_question:.4f} per-question cap\n"
            f"Models in use: {cfg.models} (n_runs={cfg.n_runs}, parser={cfg.parser_model})\n"
        )

        llms = {
            "default": GeneralLlm(model=cfg.models[0], temperature=0.3),
            "summarizer": GeneralLlm(model=cfg.parser_model, temperature=0.3),
            "researcher": GeneralLlm(model=cfg.parser_model, temperature=0.3),
            "parser": GeneralLlm(model=cfg.parser_model, temperature=0.3),
        }
        bot = FutureEvalBot(
            cfg=cfg,
            db_conn=conn,
            budget_guard=budget_guard,
            dry_run=not args.publish,
            llms=llms,
            research_reports_per_question=1,
            predictions_per_research_report=1,
            use_research_summary_to_forecast=False,
            enable_summarize_research=False,
            publish_reports_to_metaculus=args.publish,
            skip_previously_forecasted_questions=skip_previously_forecasted,
            extra_metadata_in_explanation=True,
        )

        print(f"Forecasting on {len(questions)} question(s).\n")

        reports = []
        skipped_budget = 0
        for question in questions:
            try:
                report = await bot.forecast_question(question, return_exceptions=True)
            except Exception as e:  # pragma: no cover - defensive
                report = e

            if isinstance(report, BudgetSkipped):
                skipped_budget += 1
                print(f"  ⏭️  Skipped (budget): {question.page_url} -- {report}")
                continue

            if isinstance(report, BaseException):
                reports.append(report)
                db.log_forecast(
                    conn,
                    question_id=getattr(question, "id_of_question", None),
                    post_id=getattr(question, "id_of_post", None),
                    question_title=question.question_text,
                    question_type=type(question).__name__,
                    question_url=question.page_url,
                    research_summary="",
                    raw_model_outputs=[],
                    aggregate_value=None,
                    post_calibration_value=None,
                    submitted_value=None,
                    dry_run=not args.publish,
                    models_used=cfg.models,
                    n_runs=cfg.n_runs,
                    cost_usd=0.0,
                    status="error",
                    error_message=f"{type(report).__name__}: {report}",
                )
                continue

            reports.append(report)
            actual_cost = report.price_estimate or 0.0
            budget_guard.record_actual(actual_cost)
            key = question.id_of_post or question.id_of_question or id(question)
            debug = bot.debug_log.pop(key, {})
            db.log_forecast(
                conn,
                question_id=question.id_of_question,
                post_id=question.id_of_post,
                question_title=question.question_text,
                question_type=type(question).__name__,
                question_url=question.page_url,
                research_summary=debug.get("research_summary", ""),
                raw_model_outputs=debug.get("raw_model_outputs", []),
                aggregate_value=debug.get("aggregate_value"),
                post_calibration_value=debug.get("post_calibration_value"),
                submitted_value=str(report.prediction),
                dry_run=not args.publish,
                models_used=cfg.models,
                n_runs=cfg.n_runs,
                cost_usd=actual_cost,
                status="ok",
            )
            if question.id_of_question is not None:
                db.mark_question_forecasted(conn, question.id_of_question)

        FutureEvalBot.log_report_summary(reports, raise_errors=False)
        print_run_summary_banner(
            reports, will_publish=args.publish, tournament_url=TOURNAMENT_URLS.get(args.mode)
        )
        if skipped_budget:
            print(f"⏭️   {skipped_budget} question(s) skipped due to budget guard.\n")

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run the FutureEval ensemble bot")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["tournament", "test_questions"],
        default="tournament",
        help="tournament = fall-futureeval-2026 + minibench; test_questions = bot-testing-area sandbox",
    )
    parser.add_argument(
        "--target",
        type=str,
        choices=["both", "seasonal", "minibench"],
        default="both",
        help="Only used with --mode tournament",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Actually submit forecasts to Metaculus. Default is dry-run (no submission).",
    )
    parser.add_argument(
        "--post-ids",
        type=str,
        default=None,
        help="Comma-separated Metaculus post IDs to forecast on directly, bypassing "
        "tournament listing (for reproducible re-runs or a single targeted submission).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of questions processed this run.",
    )
    parser.add_argument(
        "--type-diverse",
        action="store_true",
        help="When used with --limit, prefer a mix of question types over the first N found.",
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args)))
