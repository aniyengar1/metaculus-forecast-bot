"""
FutureEvalBot: a ForecastBot subclass that fans out each question to an
ensemble of `cfg.n_runs` model calls (round-robin over `cfg.models`), then
aggregates with our own rules instead of forecasting_tools' defaults:
  - binary: median in log-odds space
  - multiple choice: per-option median, renormalized
  - numeric/date: PCHIP-smoothed per-model CDFs, pointwise-median aggregated

A hard budget guard runs before any model is called for a question (estimate
from token counts; skip+log if it would exceed the per-question or per-day
cap). Every raw per-model output, the aggregate, the post-calibration value,
and the submitted value are stashed in `self.debug_log` keyed by question so
the driver (main.py) can write one SQLite row per question after the fact.

Fan-out happens *inside* our `_run_forecast_on_*` overrides (not via the base
class's `predictions_per_research_report` loop) because that loop's
concurrency ordering isn't a reliable way to assign "run i -> model i": we
want explicit control over which model serves which run, and over per-model
error handling/logging. We therefore always run with
`research_reports_per_question=1, predictions_per_research_report=1` -- the
base class's own aggregation step then sees a single, already-ensembled
prediction per question (a no-op pass-through).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forecasting_tools import (
    BinaryPrediction,
    BinaryQuestion,
    DatePercentile,
    DateQuestion,
    ForecastBot,
    GeneralLlm,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
    Percentile,
    PredictedOptionList,
    ReasonedPrediction,
    clean_indents,
    structure_output,
)
from forecasting_tools.data_models.numeric_report import NumericReport

from metaculus_bot import db, research
from metaculus_bot.aggregation import (
    aggregate_binary_log_odds,
    aggregate_multiple_choice_median,
    build_pchip_numeric_distribution,
)
from metaculus_bot.budget import BudgetGuard, estimate_question_cost
from metaculus_bot.calibration import calibrate_binary_probability
from metaculus_bot.config import Config
from metaculus_bot.deterministic_parse import (
    parse_binary,
    parse_date_percentiles,
    parse_multiple_choice,
    parse_numeric_percentiles,
)
from metaculus_bot.llm_retry import invoke_with_backoff

logger = logging.getLogger(__name__)

# Small per-run stagger before each ensemble model call. OpenRouter's free
# tier models share a congested upstream pool and frequently 429 when hit by
# several concurrent requests at once; spacing requests out a little cuts
# collision odds substantially without meaningfully slowing a question down.
_STAGGER_SECONDS = 1.5


class BudgetSkipped(Exception):
    """Raised (and already logged to SQLite) when a question is skipped
    because forecasting it would exceed the per-question or per-day cap."""


@dataclass
class ModelRunResult:
    model: str
    run_index: int
    raw_text: str = ""
    value: Any = None
    error: str | None = None
    parse_method: str = "deterministic"  # or "llm_fallback"


def _question_key(question: MetaculusQuestion) -> int | str:
    return question.id_of_post or question.id_of_question or id(question)


class FutureEvalBot(ForecastBot):
    # Only used on the LLM-parser fallback path (deterministic parsing in
    # metaculus_bot/deterministic_parse.py handles the common case with zero
    # extra requests). 1 rather than 2: fallback is already the rare,
    # request-expensive path, so we don't double it with a validation re-ask.
    _structure_output_validation_samples = 1

    def __init__(
        self,
        *,
        cfg: Config,
        db_conn: sqlite3.Connection,
        budget_guard: BudgetGuard,
        dry_run: bool,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.cfg = cfg
        self.db_conn = db_conn
        self.budget_guard = budget_guard
        self.dry_run = dry_run
        self.models = cfg.models
        self.n_runs = cfg.n_runs
        self.debug_log: dict[Any, dict[str, Any]] = {}

    def _model_for_run(self, i: int) -> str:
        return self.models[i % len(self.models)]

    ##################################### BUDGET #####################################

    def _check_budget_or_raise(self, question: MetaculusQuestion) -> None:
        estimated = estimate_question_cost(self.cfg)
        ok, reason = self.budget_guard.check(estimated)
        if ok:
            return
        logger.warning(f"Skipping question {question.page_url} due to budget guard: {reason}")
        db.log_forecast(
            self.db_conn,
            question_id=question.id_of_question,
            post_id=question.id_of_post,
            question_title=question.question_text,
            question_type=type(question).__name__,
            question_url=question.page_url,
            research_summary="",
            raw_model_outputs=[],
            aggregate_value=None,
            post_calibration_value=None,
            submitted_value=None,
            dry_run=self.dry_run,
            models_used=self.models,
            n_runs=self.n_runs,
            cost_usd=0.0,
            status="skipped_budget",
            error_message=reason,
        )
        raise BudgetSkipped(reason)

    ##################################### RESEARCH #####################################

    async def run_research(self, question: MetaculusQuestion) -> str:
        self._check_budget_or_raise(question)
        summarizer_llm = self.get_llm("parser", "llm")
        summary = await research.gather_research(
            question, self.cfg, self.db_conn, summarizer_llm
        )
        self.debug_log[_question_key(question)] = {
            "research_summary": summary,
            "raw_model_outputs": [],
        }
        return summary

    ##################################### BINARY #####################################

    @staticmethod
    def _binary_prompt(question: BinaryQuestion, research_text: str) -> str:
        return clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Question background:
            {question.background_info}

            This question's outcome will be determined by the specific criteria below. These criteria have not yet been satisfied:
            {question.resolution_criteria}

            {question.fine_print}

            Your research assistant says:
            {research_text}

            Today is {datetime.now().strftime("%Y-%m-%d")}.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The status quo outcome if nothing changed.
            (c) A brief description of a scenario that results in a No outcome.
            (d) A brief description of a scenario that results in a Yes outcome.

            You write your rationale remembering that good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time.

            The last thing you write is your final answer as: "Probability: ZZ%", 0-100
            """
        )

    async def _single_binary_run(
        self, question: BinaryQuestion, research_text: str, model_name: str, run_index: int
    ) -> ModelRunResult:
        try:
            await asyncio.sleep(run_index * _STAGGER_SECONDS)
            llm = GeneralLlm(model=model_name, temperature=0.3, allowed_tries=3)
            prompt = self._binary_prompt(question, research_text)
            reasoning = await invoke_with_backoff(llm, prompt)

            value = parse_binary(reasoning)
            parse_method = "deterministic"
            if value is None:
                logger.warning(
                    f"Deterministic binary parse failed (model={model_name}, run={run_index}), "
                    "falling back to LLM parser"
                )
                parsed: BinaryPrediction = await structure_output(
                    reasoning,
                    BinaryPrediction,
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                )
                value = max(0.01, min(0.99, parsed.prediction_in_decimal))
                parse_method = "llm_fallback"

            return ModelRunResult(
                model=model_name, run_index=run_index, raw_text=reasoning, value=value, parse_method=parse_method
            )
        except Exception as e:
            logger.warning(f"Model run failed (binary, model={model_name}, run={run_index}): {e}")
            return ModelRunResult(model=model_name, run_index=run_index, error=f"{type(e).__name__}: {e}")

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        tasks = [
            self._single_binary_run(question, research, self._model_for_run(i), i)
            for i in range(self.n_runs)
        ]
        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r.error is None]
        if not successes:
            raise RuntimeError(
                f"All {self.n_runs} ensemble runs failed for binary question {question.page_url}: "
                f"{[r.error for r in results]}"
            )

        aggregate = aggregate_binary_log_odds([r.value for r in successes])
        calibrated = calibrate_binary_probability(
            aggregate,
            self.cfg.calibration_k,
            self.cfg.calibration_clip_min,
            self.cfg.calibration_clip_max,
        )

        self._record_debug(question, results, aggregate, calibrated)
        combined_reasoning = self._combine_reasoning(results)
        return ReasonedPrediction(prediction_value=calibrated, reasoning=combined_reasoning)

    ##################################### MULTIPLE CHOICE #####################################

    @staticmethod
    def _mc_prompt(question: MultipleChoiceQuestion, research_text: str) -> str:
        return clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            The options are: {question.options}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Your research assistant says:
            {research_text}

            Today is {datetime.now().strftime("%Y-%m-%d")}.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The status quo outcome if nothing changed.
            (c) A description of a scenario that results in an unexpected outcome.

            You write your rationale remembering that (1) good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time, and (2) good forecasters leave some moderate probability on most options to account for unexpected outcomes.

            The last thing you write is your final probabilities for the N options in this order {question.options} as:
            Option_A: Probability_A
            Option_B: Probability_B
            ...
            Option_N: Probability_N
            """
        )

    async def _single_mc_run(
        self, question: MultipleChoiceQuestion, research_text: str, model_name: str, run_index: int
    ) -> ModelRunResult:
        try:
            await asyncio.sleep(run_index * _STAGGER_SECONDS)
            llm = GeneralLlm(model=model_name, temperature=0.3, allowed_tries=3)
            prompt = self._mc_prompt(question, research_text)
            reasoning = await invoke_with_backoff(llm, prompt)

            parsed = parse_multiple_choice(reasoning, question.options)
            parse_method = "deterministic"
            if parsed is None:
                logger.warning(
                    f"Deterministic MC parse failed (model={model_name}, run={run_index}), "
                    "falling back to LLM parser"
                )
                parsing_instructions = clean_indents(
                    f"""
                    Make sure that all option names are one of the following:
                    {question.options}
                    Additionally, you may sometimes need to parse a 0% probability. Please do not skip options with 0% but rather make it an entry in your final list with 0% probability.
                    """
                )
                parsed = await structure_output(
                    text_to_structure=reasoning,
                    output_type=PredictedOptionList,
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                    additional_instructions=parsing_instructions,
                )
                parse_method = "llm_fallback"

            return ModelRunResult(
                model=model_name, run_index=run_index, raw_text=reasoning, value=parsed, parse_method=parse_method
            )
        except Exception as e:
            logger.warning(f"Model run failed (MC, model={model_name}, run={run_index}): {e}")
            return ModelRunResult(model=model_name, run_index=run_index, error=f"{type(e).__name__}: {e}")

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        tasks = [
            self._single_mc_run(question, research, self._model_for_run(i), i)
            for i in range(self.n_runs)
        ]
        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r.error is None]
        if not successes:
            raise RuntimeError(
                f"All {self.n_runs} ensemble runs failed for MC question {question.page_url}: "
                f"{[r.error for r in results]}"
            )

        aggregate = aggregate_multiple_choice_median([r.value for r in successes])
        self._record_debug(question, results, aggregate.to_dict(), aggregate.to_dict())
        combined_reasoning = self._combine_reasoning(results)
        return ReasonedPrediction(prediction_value=aggregate, reasoning=combined_reasoning)

    ##################################### NUMERIC / DATE #####################################

    @staticmethod
    def _bound_messages(question: NumericQuestion | DateQuestion) -> tuple[str, str]:
        if isinstance(question, DateQuestion):
            upper_bound_number = question.upper_bound.date().isoformat()
            lower_bound_number = question.lower_bound.date().isoformat()
            unit = ""
        else:
            upper_bound_number = question.nominal_upper_bound if question.nominal_upper_bound is not None else question.upper_bound
            lower_bound_number = question.nominal_lower_bound if question.nominal_lower_bound is not None else question.lower_bound
            unit = question.unit_of_measure or ""

        if question.open_upper_bound:
            upper_msg = f"The question creator thinks the number is likely not higher than {upper_bound_number} {unit}."
        else:
            upper_msg = f"The outcome can not be higher than {upper_bound_number} {unit}."
        if question.open_lower_bound:
            lower_msg = f"The question creator thinks the number is likely not lower than {lower_bound_number} {unit}."
        else:
            lower_msg = f"The outcome can not be lower than {lower_bound_number} {unit}."
        return upper_msg, lower_msg

    def _numeric_prompt(self, question: NumericQuestion, research_text: str) -> str:
        upper_msg, lower_msg = self._bound_messages(question)
        return clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Units for answer: {question.unit_of_measure if question.unit_of_measure else "Not stated (please infer this)"}

            Your research assistant says:
            {research_text}

            Today is {datetime.now().strftime("%Y-%m-%d")}.

            {lower_msg}
            {upper_msg}

            Formatting Instructions:
            - Please notice the units requested and give your answer in these units.
            - Never use scientific notation.
            - Always start with a smaller number and then increase from there.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) The expectations of experts and markets.
            (e) A brief description of an unexpected scenario that results in a low outcome.
            (f) A brief description of an unexpected scenario that results in a high outcome.

            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: XX (lowest number value)
            Percentile 20: XX
            Percentile 40: XX
            Percentile 60: XX
            Percentile 80: XX
            Percentile 90: XX (highest number value)
            "
            """
        )

    def _date_prompt(self, question: DateQuestion, research_text: str) -> str:
        upper_msg, lower_msg = self._bound_messages(question)
        return clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Your research assistant says:
            {research_text}

            Today is {datetime.now().strftime("%Y-%m-%d")}.

            {lower_msg}
            {upper_msg}

            Formatting Instructions:
            - This is a date question, and as such, the answer must be expressed in terms of dates.
            - The dates must be written in the format of YYYY-MM-DD.
            - Always start with a lower date chronologically and then increase from there.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) The expectations of experts and markets.
            (e) A brief description of an unexpected scenario that results in a low outcome.
            (f) A brief description of an unexpected scenario that results in a high outcome.

            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: YYYY-MM-DD (oldest date)
            Percentile 20: YYYY-MM-DD
            Percentile 40: YYYY-MM-DD
            Percentile 60: YYYY-MM-DD
            Percentile 80: YYYY-MM-DD
            Percentile 90: YYYY-MM-DD (newest date)
            "
            """
        )

    async def _single_numeric_run(
        self, question: NumericQuestion, research_text: str, model_name: str, run_index: int
    ) -> ModelRunResult:
        try:
            await asyncio.sleep(run_index * _STAGGER_SECONDS)
            llm = GeneralLlm(model=model_name, temperature=0.3, allowed_tries=3)
            prompt = self._numeric_prompt(question, research_text)
            reasoning = await invoke_with_backoff(llm, prompt)

            percentiles = parse_numeric_percentiles(reasoning)
            parse_method = "deterministic"
            if percentiles is None:
                logger.warning(
                    f"Deterministic numeric parse failed (model={model_name}, run={run_index}), "
                    "falling back to LLM parser"
                )
                percentiles = await structure_output(
                    reasoning,
                    list[Percentile],
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                )
                parse_method = "llm_fallback"

            distribution = build_pchip_numeric_distribution(percentiles, question)
            return ModelRunResult(
                model=model_name,
                run_index=run_index,
                raw_text=reasoning,
                value=distribution,
                parse_method=parse_method,
            )
        except Exception as e:
            logger.warning(f"Model run failed (numeric, model={model_name}, run={run_index}): {e}")
            return ModelRunResult(model=model_name, run_index=run_index, error=f"{type(e).__name__}: {e}")

    async def _run_forecast_on_numeric(
        self, question: NumericQuestion, research: str
    ) -> ReasonedPrediction:
        tasks = [
            self._single_numeric_run(question, research, self._model_for_run(i), i)
            for i in range(self.n_runs)
        ]
        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r.error is None]
        if not successes:
            raise RuntimeError(
                f"All {self.n_runs} ensemble runs failed for numeric question {question.page_url}: "
                f"{[r.error for r in results]}"
            )

        aggregate = await NumericReport.aggregate_predictions(
            [r.value for r in successes], question
        )
        self._record_debug(
            question, results, aggregate.get_representative_percentiles(), aggregate.get_representative_percentiles()
        )
        combined_reasoning = self._combine_reasoning(results)
        return ReasonedPrediction(prediction_value=aggregate, reasoning=combined_reasoning)

    async def _single_date_run(
        self, question: DateQuestion, research_text: str, model_name: str, run_index: int
    ) -> ModelRunResult:
        try:
            await asyncio.sleep(run_index * _STAGGER_SECONDS)
            llm = GeneralLlm(model=model_name, temperature=0.3, allowed_tries=3)
            prompt = self._date_prompt(question, research_text)
            reasoning = await invoke_with_backoff(llm, prompt)

            date_percentiles = parse_date_percentiles(reasoning)
            parse_method = "deterministic"
            if date_percentiles is None:
                logger.warning(
                    f"Deterministic date parse failed (model={model_name}, run={run_index}), "
                    "falling back to LLM parser"
                )
                date_percentiles = await structure_output(
                    reasoning,
                    list[DatePercentile],
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                )
                parse_method = "llm_fallback"

            percentiles = [
                Percentile(percentile=dp.percentile, value=dp.value.timestamp())
                for dp in date_percentiles
            ]
            distribution = build_pchip_numeric_distribution(percentiles, question)
            return ModelRunResult(
                model=model_name,
                run_index=run_index,
                raw_text=reasoning,
                value=distribution,
                parse_method=parse_method,
            )
        except Exception as e:
            logger.warning(f"Model run failed (date, model={model_name}, run={run_index}): {e}")
            return ModelRunResult(model=model_name, run_index=run_index, error=f"{type(e).__name__}: {e}")

    async def _run_forecast_on_date(
        self, question: DateQuestion, research: str
    ) -> ReasonedPrediction:
        tasks = [
            self._single_date_run(question, research, self._model_for_run(i), i)
            for i in range(self.n_runs)
        ]
        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r.error is None]
        if not successes:
            raise RuntimeError(
                f"All {self.n_runs} ensemble runs failed for date question {question.page_url}: "
                f"{[r.error for r in results]}"
            )

        aggregate = await NumericReport.aggregate_predictions(
            [r.value for r in successes], question
        )
        self._record_debug(
            question, results, aggregate.get_representative_percentiles(), aggregate.get_representative_percentiles()
        )
        combined_reasoning = self._combine_reasoning(results)
        return ReasonedPrediction(prediction_value=aggregate, reasoning=combined_reasoning)

    ##################################### SHARED HELPERS #####################################

    def _record_debug(
        self,
        question: MetaculusQuestion,
        results: list[ModelRunResult],
        aggregate_value: Any,
        post_calibration_value: Any,
    ) -> None:
        key = _question_key(question)
        entry = self.debug_log.setdefault(key, {"research_summary": "", "raw_model_outputs": []})
        entry["raw_model_outputs"] = [
            {
                "model": r.model,
                "run_index": r.run_index,
                "raw_text": (r.raw_text or "")[:4000],
                "parsed_value": str(r.value) if r.value is not None else None,
                "error": r.error,
                "parse_method": r.parse_method if r.error is None else None,
            }
            for r in results
        ]
        entry["aggregate_value"] = str(aggregate_value)
        entry["post_calibration_value"] = str(post_calibration_value)

    @staticmethod
    def _combine_reasoning(results: list[ModelRunResult]) -> str:
        parts = []
        for r in results:
            if r.error:
                parts.append(f"### Model: {r.model} (run {r.run_index}) -- FAILED: {r.error}")
            else:
                parts.append(f"### Model: {r.model} (run {r.run_index})\n{r.raw_text}")
        return "\n\n".join(parts)
