import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import dotenv

from bot_helpers import (
    check_environment,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
)

silence_noisy_dependencies()

from forecasting_tools import (  # noqa: E402
    ApiFilter,
    BinaryQuestion,
    DateQuestion,
    DiscreteQuestion,
    GeneralLlm,
    MetaculusClient,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
)
from forecasting_tools.data_models.questions import QuestionState  # noqa: E402

from metaculus_bot import db  # noqa: E402
from metaculus_bot.bot import BudgetSkipped, FutureEvalBot, InvalidDistributionSkipped  # noqa: E402
from metaculus_bot.budget import BudgetGuard  # noqa: E402
from metaculus_bot.config import load_config  # noqa: E402
from metaculus_bot.model_health import select_healthy_models  # noqa: E402
from metaculus_bot.openrouter_quota import get_free_model_daily_quota  # noqa: E402
from metaculus_bot.provider_quota import (  # noqa: E402
    compute_daily_question_budget,
    split_real_and_estimated_cost,
)

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

# Was: os.getenv("GITHUB_EVENT_NAME") == "schedule" -- correct back when
# workflow_dispatch only ever meant a human manually testing in the GH UI.
# Broke 2026-10-19: the GCP VM backup poller (docs/STAGE_B_PLAN.md "Backup
# poller", added because GitHub's own schedule: trigger was silently
# skipping ~96% of ticks) also fires via workflow_dispatch every ~10 min --
# so once OpenRouter quota dipped below the reserve threshold partway
# through the day, this guard misclassified every one of *those* (the
# actual production runs) as "local/manual" and refused to start, while
# the few remaining native schedule: ticks kept succeeding. GITHUB_ACTIONS
# is set to "true" for every GitHub Actions run regardless of trigger event
# (schedule or workflow_dispatch) and unset when run locally -- that's the
# real distinction this guard needs: "CI run" vs "someone's laptop," not
# "which trigger fired it."
IS_CI_RUN = os.getenv("GITHUB_ACTIONS") == "true"


def _check_rate_limit_guard(cfg) -> bool:
    """Returns False if the run should abort. Never calls sys.exit itself --
    asyncio.run()/nest_asyncio don't propagate SystemExit out of a task
    cleanly (it surfaces as an ugly 'Task exception was never retrieved'
    instead of a clean exit), so the actual process exit happens once in
    __main__, after the event loop has finished."""
    if IS_CI_RUN:
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


async def _fetch_one_tournament_with_diagnostics(
    client: MetaculusClient, tournament_id: int | str, label: str
) -> tuple[list[MetaculusQuestion], list[MetaculusQuestion]]:
    """Fetches EVERY question in the tournament regardless of status (not
    just open ones), logs a full breakdown (total / by status / open-but-
    already-forecasted / open-and-still-needs-forecasting), and returns
    (not_yet_forecasted, already_forecasted) -- both restricted to open
    questions. Callers decide what to do with already-forecasted ones (for
    MiniBench: nothing, drop them; for seasonal: maybe re-forecast if stale,
    see _select_reforecast_candidates). This split also means 'Returning 0
    questions' in forecasting_tools' own logs (which only ever reports the
    post-filter count) is distinguishable from a filter bug: if our own
    unfiltered count also says 0 (or 0 open), the filter is working
    correctly and there's just nothing to forecast right now."""
    api_filter = ApiFilter(allowed_tournaments=[tournament_id])  # no status/type restriction
    all_questions = await client.get_questions_matching_filter(api_filter)

    by_status = Counter(q.state.value if q.state else "unknown" for q in all_questions)
    open_questions = [q for q in all_questions if q.state == QuestionState.OPEN]
    already_forecasted = [q for q in open_questions if q.already_forecasted]
    not_yet_forecasted = [q for q in open_questions if not q.already_forecasted]

    status_breakdown = ", ".join(f"{status}={count}" for status, count in by_status.items())
    print(
        f"[{label}] tournament breakdown: total={len(all_questions)} ({status_breakdown or 'none'}) "
        f"-- of {len(open_questions)} open: {len(already_forecasted)} already forecasted, "
        f"{len(not_yet_forecasted)} still need forecasting"
    )
    return not_yet_forecasted, already_forecasted


async def _select_reforecast_candidates(
    already_forecasted: list[MetaculusQuestion], conn, cfg
) -> list[MetaculusQuestion]:
    """Still-open seasonal questions we've already forecast, but whose last
    forecast is stale enough to redo (new information may have changed
    things) -- oldest-forecast-first, capped at
    cfg.max_reforecasts_per_day *for the whole day across all runs*, not
    per-run."""
    if not already_forecasted:
        return []
    now = datetime.now(timezone.utc)
    staleness = timedelta(days=cfg.reforecast_staleness_days)
    stale: list[tuple[datetime, MetaculusQuestion]] = []
    for q in already_forecasted:
        last = db.get_last_forecast_time(conn, q.id_of_question)
        if last and (now - last) > staleness:
            stale.append((last, q))
    stale.sort(key=lambda pair: pair[0])  # oldest-forecast-first

    done_today = db.count_reforecasts_today(conn)
    remaining_cap = max(cfg.max_reforecasts_per_day - done_today, 0)
    return [q for _, q in stale[:remaining_cap]]


def _check_closed_unforecast(conn) -> None:
    """Checked at every exit point of _run (not just the happy path after a
    full forecast loop), so a question that closed while deferred across
    runs is caught even on a run that aborts early for an unrelated reason
    (e.g. <2 healthy models)."""
    newly_closed = db.find_newly_closed_unforecast(conn)
    for question_id, title, url, close_time in newly_closed:
        msg = f"Question closed without ever being forecast: {url} ({title}) -- closed {close_time}"
        logger.warning(msg)
        db.log_alert(conn, msg)
        print(f"🚨 ALERT: {msg}")
        db.mark_closed_unforecast_alerted(conn, question_id)


async def _fetch_questions(
    client: MetaculusClient, mode: str, target: str, cfg, conn
) -> list[MetaculusQuestion]:
    if mode == "test_questions":
        not_yet, _ = await _fetch_one_tournament_with_diagnostics(
            client, "bot-testing-area", "bot-testing-area"
        )
        return not_yet

    questions: list[MetaculusQuestion] = []
    if target in ("both", "seasonal"):
        if cfg.enable_seasonal_tournament:
            not_yet, already = await _fetch_one_tournament_with_diagnostics(
                client, client.CURRENT_AI_COMPETITION_ID, "seasonal"
            )
            questions += not_yet
            reforecast_candidates = await _select_reforecast_candidates(already, conn, cfg)
            if reforecast_candidates:
                print(
                    f"[seasonal] re-forecasting {len(reforecast_candidates)} stale question(s) "
                    f"(last forecast >{cfg.reforecast_staleness_days}d ago)"
                )
            questions += reforecast_candidates
        else:
            print(
                "  (seasonal tournament skipped: ENABLE_SEASONAL_TOURNAMENT is False "
                "-- set it once paid credits + stronger models are confirmed)"
            )
    if target in ("both", "minibench"):
        not_yet, _ = await _fetch_one_tournament_with_diagnostics(
            client, client.CURRENT_MINIBENCH_ID, "minibench"
        )
        questions += not_yet
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
        if args.mode == "test_questions":
            tournament_label = "test"
        elif args.post_ids:
            tournament_label = "manual"
        else:
            tournament_label = args.target  # "seasonal" | "minibench" | "both"

        client = MetaculusClient()
        if args.post_ids:
            ids = [int(x.strip()) for x in args.post_ids.split(",") if x.strip()]
            questions = [client.get_question_by_post_id(pid) for pid in ids]
            # The normal fetch path (_fetch_questions) already decides
            # per-tournament what to do with already-forecasted questions
            # (drop for MiniBench, maybe re-forecast for seasonal); the
            # --post-ids bypass doesn't go through that, so apply the simple
            # (non-reforecasting) rule here as a safety net.
            if args.mode != "test_questions":
                for q in questions:
                    if q.already_forecasted:
                        db.mark_question_forecasted(conn, q.id_of_question)
                questions = [q for q in questions if not q.already_forecasted]
        else:
            questions = await _fetch_questions(client, args.mode, args.target, cfg, conn)

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

        # Note in seen_questions that these were already forecast (so the
        # "closed without being forecast" alert below doesn't
        # false-positive on them) -- this does NOT filter `questions`:
        # _fetch_questions already decided per-tournament what to do with
        # already-forecasted ones (drop for MiniBench, maybe keep a few
        # stale ones to re-forecast for seasonal).
        for q in questions:
            if q.already_forecasted:
                db.mark_question_forecasted(conn, q.id_of_question)

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

        if not questions:
            print("Nothing to forecast this run (no new questions).\n")
            _check_closed_unforecast(conn)
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
            _check_closed_unforecast(conn)
            return 1

        print(f"Health-checking parser pool ({len(cfg.parser_model_pool)} candidates)...")
        parser_candidates, parser_health_results = await select_healthy_models(
            cfg.parser_model_pool, target_count=1
        )
        for r in parser_health_results:
            status = "healthy" if r.healthy else f"DEAD -- {r.error}"
            print(f"  {r.model:<55} {status}")
        parser_model = parser_candidates[0] if parser_candidates else selected_models[0]
        print(f"Parser/summarizer model: {parser_model}\n")

        effective_n_runs = (
            cfg.target_ensemble_size if len(selected_models) >= cfg.target_ensemble_size else len(selected_models)
        )
        cfg = replace(
            cfg,
            models=selected_models,
            n_runs=effective_n_runs,
            parser_model=parser_model,
        )

        # Real per-provider remaining quota, not a static guess -- see
        # metaculus_bot/provider_quota.py. Computed only now because it needs
        # to know which specific models (incl. the parser) are actually in
        # play this run. MiniBench gets priority on the shared quota (it
        # polls every 15 min against ~3h question windows; seasonal polls
        # hourly against multi-day windows) by having the seasonal run
        # deliberately reserve some headroom rather than spend down to zero.
        minibench_reserve = cfg.minibench_quota_reserve if tournament_label == "seasonal" else 0
        daily_budget = compute_daily_question_budget(
            selected_models,
            parser_model,
            conn,
            ceiling=cfg.max_questions_per_day_ceiling,
            reserve=minibench_reserve,
        )
        questions_today = db.get_questions_forecast_today(conn)
        remaining_quota = max(daily_budget - questions_today, 0)
        print(
            f"Computed daily question budget: {daily_budget} (ceiling {cfg.max_questions_per_day_ceiling}"
            f"{f', minibench reserve {minibench_reserve}' if minibench_reserve else ''}), "
            f"{questions_today} already forecast today -> {remaining_quota} remaining.\n"
        )
        if remaining_quota < len(questions):
            print(
                f"Throttling: processing the {remaining_quota} soonest-closing of {len(questions)} "
                "found, rest deferred to a later run (not dropped -- will be retried, and alerted "
                "on if one closes first)."
            )
            questions = questions[:remaining_quota]

        if not questions:
            print("Nothing to forecast this run (daily quota-derived cap reached).\n")
            _check_closed_unforecast(conn)
            return 0

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
            # False unconditionally: main.py is the sole authority on which
            # questions to attempt (per-tournament already-forecasted
            # handling, including deliberate seasonal re-forecasts, all
            # happens above in _fetch_questions/_select_reforecast_candidates).
            # The base class's own already_forecasted filter would otherwise
            # silently re-drop a question we specifically chose to re-forecast.
            skip_previously_forecasted_questions=False,
            extra_metadata_in_explanation=True,
        )

        print(f"Forecasting on {len(questions)} question(s).\n")

        reports = []
        skipped_budget = 0
        skipped_invalid_distribution = 0
        successful_count = 0
        attempted_count = len(questions)
        for question in questions:
            try:
                report = await bot.forecast_question(question, return_exceptions=True)
            except Exception as e:  # pragma: no cover - defensive
                report = e

            if isinstance(report, BudgetSkipped):
                skipped_budget += 1
                print(f"  ⏭️  Skipped (budget): {question.page_url} -- {report}")
                continue

            if isinstance(report, InvalidDistributionSkipped):
                skipped_invalid_distribution += 1
                print(f"  🚫 Skipped (invalid distribution): {question.page_url} -- {report}")
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
                    estimated_cost_usd=0.0,
                    tournament=tournament_label,
                    status="error",
                    error_message=f"{type(report).__name__}: {report}",
                )
                continue

            reports.append(report)
            successful_count += 1
            key = question.id_of_post or question.id_of_question or id(question)
            debug = bot.debug_log.pop(key, {})

            estimated_cost = report.price_estimate or 0.0
            models_used_this_question = {
                o["model"] for o in debug.get("raw_model_outputs", []) if not o.get("error")
            }
            models_used_this_question.add(cfg.parser_model)
            real_cost, estimated_cost = split_real_and_estimated_cost(
                models_used_this_question, estimated_cost
            )
            budget_guard.record_actual(real_cost)

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
                cost_usd=real_cost,
                estimated_cost_usd=estimated_cost,
                tournament=tournament_label,
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
        if skipped_invalid_distribution:
            print(
                f"🚫   {skipped_invalid_distribution} question(s) skipped -- aggregated forecast "
                "failed the pre-submit sanity guard (see alerts).\n"
            )

        if attempted_count > 0 and successful_count == 0:
            alert_msg = (
                f"Attempted {attempted_count} open question(s) this run but forecast 0 "
                "successfully -- model pool was healthy enough to start, so every individual "
                "attempt must have failed/errored. See the per-question errors above."
            )
            logger.error(alert_msg)
            db.log_alert(conn, alert_msg)
            print(f"🚨 ALERT: {alert_msg}\n")

        # Checked last, not before the forecast loop above: a question can
        # be already-closed-but-not-yet-forecast at the *start* of this very
        # run and still get successfully forecast by the loop above (e.g.
        # --post-ids against a closed question, or a late health-check-pool
        # recovery) -- checking before the loop produced false positives on
        # exactly those questions.
        _check_closed_unforecast(conn)

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
