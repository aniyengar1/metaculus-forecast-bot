"""Tests for metaculus_bot.bot._structured_parsed_value and the
raw_model_outputs it feeds into via _record_debug -- the fix for a real
data-quality gap: raw_text was truncated to 4000 chars for storage, and
parsed_value was just str(value) (a Python repr, not valid JSON for
anything but binary), so later analysis of multiple-choice/numeric/date
model outputs had to re-parse raw_text, which the truncation could break
entirely (confirmed 2026-10-17/18 against a real question). Now
parsed_value is a clean, directly-usable JSON structure per type, stored
independent of raw_text."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from forecasting_tools import (
    DateQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

from metaculus_bot.bot import ModelRunResult, _structured_parsed_value, FutureEvalBot


def test_structured_parsed_value_binary_is_plain_float():
    result = ModelRunResult(model="m", run_index=0, value=0.62)
    assert _structured_parsed_value(result) == 0.62
    assert isinstance(_structured_parsed_value(result), float)


def test_structured_parsed_value_multiple_choice_is_option_dict():
    value = PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name="Low", probability=0.3),
            PredictedOption(option_name="High", probability=0.7),
        ]
    )
    result = ModelRunResult(model="m", run_index=0, value=value)
    parsed = _structured_parsed_value(result)
    assert parsed == {"Low": 0.3, "High": 0.7}


NUMERIC_QUESTION = NumericQuestion(
    id_of_question=1, id_of_post=1, question_text="q", page_url="https://x",
    upper_bound=6.0, lower_bound=3.5, open_upper_bound=True, open_lower_bound=True, zero_point=None,
)


def test_structured_parsed_value_numeric_is_sparse_percentile_list_not_full_cdf():
    elicited = [
        Percentile(percentile=0.1, value=4.0),
        Percentile(percentile=0.5, value=4.2),
        Percentile(percentile=0.9, value=4.5),
    ]
    dist = NumericDistribution.from_question(elicited, NUMERIC_QUESTION)
    result = ModelRunResult(model="m", run_index=0, value=dist, parsed_percentiles=elicited)
    parsed = _structured_parsed_value(result)
    assert parsed == [
        {"percentile": 0.1, "value": 4.0},
        {"percentile": 0.5, "value": 4.2},
        {"percentile": 0.9, "value": 4.5},
    ]
    # Sparse (3 points), not the ~201-point expanded CDF -- the whole point.
    assert len(parsed) == 3


def test_structured_parsed_value_numeric_without_parsed_percentiles_returns_none():
    """Defensive: if parsed_percentiles wasn't threaded through for some
    reason, don't fall back to a lossy str(NumericDistribution)."""
    elicited = [Percentile(percentile=0.1, value=4.0), Percentile(percentile=0.9, value=4.5)]
    dist = NumericDistribution.from_question(elicited, NUMERIC_QUESTION)
    result = ModelRunResult(model="m", run_index=0, value=dist, parsed_percentiles=None)
    assert _structured_parsed_value(result) is None


def test_structured_parsed_value_date_uses_timestamp_values():
    elicited = [
        Percentile(percentile=0.1, value=datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()),
        Percentile(percentile=0.9, value=datetime(2027, 5, 1, tzinfo=timezone.utc).timestamp()),
    ]
    date_question = DateQuestion(
        id_of_question=2, id_of_post=2, question_text="q", page_url="https://x",
        upper_bound=datetime(2027, 5, 28, tzinfo=timezone.utc),
        lower_bound=datetime(2022, 5, 29, tzinfo=timezone.utc),
        open_upper_bound=True, open_lower_bound=False, zero_point=None,
    )
    dist = NumericDistribution.from_question(elicited, date_question)
    result = ModelRunResult(model="m", run_index=0, value=dist, parsed_percentiles=elicited)
    parsed = _structured_parsed_value(result)
    assert parsed[0]["value"] == elicited[0].value
    assert parsed[1]["value"] == elicited[1].value


def test_record_debug_stores_clean_parsed_value_and_does_not_truncate_below_20000():
    fake_self = SimpleNamespace(debug_log={})
    long_text = "x" * 15000  # longer than the old 4000 cap, within the new 20000 one
    results = [ModelRunResult(model="m1", run_index=0, raw_text=long_text, value=0.4)]

    FutureEvalBot._record_debug(fake_self, SimpleNamespace(id_of_post=1, id_of_question=1), results, 0.4, 0.4)

    entry = next(iter(fake_self.debug_log.values()))
    stored = entry["raw_model_outputs"][0]
    assert len(stored["raw_text"]) == 15000  # not truncated
    assert stored["parsed_value"] == 0.4


def test_record_debug_still_bounds_a_pathological_runaway_response():
    fake_self = SimpleNamespace(debug_log={})
    runaway_text = "x" * 50000  # beyond the new 20000 safety cap
    results = [ModelRunResult(model="m1", run_index=0, raw_text=runaway_text, value=0.4)]

    FutureEvalBot._record_debug(fake_self, SimpleNamespace(id_of_post=1, id_of_question=1), results, 0.4, 0.4)

    entry = next(iter(fake_self.debug_log.values()))
    stored = entry["raw_model_outputs"][0]
    assert len(stored["raw_text"]) == 20000


def test_record_debug_parsed_value_none_on_error():
    fake_self = SimpleNamespace(debug_log={})
    results = [ModelRunResult(model="m1", run_index=0, value=None, error="RateLimitError: x")]

    FutureEvalBot._record_debug(fake_self, SimpleNamespace(id_of_post=1, id_of_question=1), results, None, None)

    entry = next(iter(fake_self.debug_log.values()))
    assert entry["raw_model_outputs"][0]["parsed_value"] is None
