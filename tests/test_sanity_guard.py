"""Tests for metaculus_bot/sanity_guard.py's pre-submit guard: the second,
independent line of defense against the 2026-10-16 incident (a date
question's median computed to a date before the question even opened).
Uses the real Ukraine martial-law question's bounds (post 43324)."""
from __future__ import annotations

from datetime import datetime, timezone

from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile

from metaculus_bot.sanity_guard import check_numeric_or_date_distribution

UKRAINE_DATE_QUESTION = DateQuestion(
    id_of_question=43326,
    id_of_post=43324,
    question_text="When will martial law be lifted in at least 3/4 of Ukraine?",
    page_url="https://www.metaculus.com/questions/43324",
    upper_bound=datetime(2027, 5, 28, 21, 0, tzinfo=timezone.utc),
    lower_bound=datetime(2022, 5, 29, tzinfo=timezone.utc),
    open_upper_bound=True,
    open_lower_bound=False,
    zero_point=None,
)

GAS_PRICE_NUMERIC_QUESTION = NumericQuestion(
    id_of_question=46142,
    id_of_post=45960,
    question_text="What will the US national average gas price be?",
    page_url="https://www.metaculus.com/questions/45960",
    upper_bound=6.0,
    lower_bound=3.5,
    open_upper_bound=True,
    open_lower_bound=True,
    zero_point=None,
)


def _date_distribution(percentiles: list[Percentile]) -> NumericDistribution:
    return NumericDistribution.from_question(percentiles, UKRAINE_DATE_QUESTION)


def _numeric_distribution(percentiles: list[Percentile]) -> NumericDistribution:
    return NumericDistribution.from_question(percentiles, GAS_PRICE_NUMERIC_QUESTION)


def _ts(date_str: str) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def test_rejects_the_real_incident_median_before_question_opened():
    """The exact bug: a median in the past relative to 'now', even though
    it's comfortably within the question's own [2022, 2027] bound range."""
    percentiles = [
        Percentile(percentile=0.10, value=_ts("2023-01-01")),
        Percentile(percentile=0.50, value=_ts("2024-11-26")),  # the actual bad median
        Percentile(percentile=0.90, value=_ts("2026-01-01")),
    ]
    dist = _date_distribution(percentiles)
    reason = check_numeric_or_date_distribution(dist, UKRAINE_DATE_QUESTION, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert reason is not None
    assert "not after now" in reason


def test_accepts_a_sane_date_distribution():
    percentiles = [
        Percentile(percentile=0.10, value=_ts("2026-11-01")),
        Percentile(percentile=0.50, value=_ts("2027-02-01")),
        Percentile(percentile=0.90, value=_ts("2027-05-20")),
    ]
    dist = _date_distribution(percentiles)
    reason = check_numeric_or_date_distribution(dist, UKRAINE_DATE_QUESTION, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert reason is None


def test_rejects_median_outside_bounds():
    """Backstop case: a value that ends up genuinely outside
    [lower_bound, upper_bound] (shouldn't happen for a well-formed
    distribution, but must be caught if it ever does)."""
    percentiles = [
        Percentile(percentile=0.10, value=3.6),
        Percentile(percentile=0.50, value=4.0),
        Percentile(percentile=0.90, value=4.5),
    ]
    dist = _numeric_distribution(percentiles)
    # Manually corrupt the median percentile's value past the bound to
    # simulate the backstop case directly, bypassing NumericDistribution's
    # own construction (which would normally clamp it).
    for p in dist.declared_percentiles:
        if abs(p.percentile - 0.5) < 1e-9:
            p.value = 100.0  # miles outside [3.5, 6.0]
    reason = check_numeric_or_date_distribution(dist, GAS_PRICE_NUMERIC_QUESTION, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert reason is not None
    assert "outside the question's range" in reason


def test_accepts_a_sane_numeric_distribution():
    percentiles = [
        Percentile(percentile=0.10, value=4.0),
        Percentile(percentile=0.50, value=4.2),
        Percentile(percentile=0.90, value=4.5),
    ]
    dist = _numeric_distribution(percentiles)
    reason = check_numeric_or_date_distribution(dist, GAS_PRICE_NUMERIC_QUESTION, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert reason is None


def test_date_p10_before_now_is_fine_only_median_matters():
    """The 'after now' check is median-only -- P10 being close to or even
    slightly before now is legitimate (the question could resolve soon)."""
    percentiles = [
        Percentile(percentile=0.10, value=_ts("2026-10-01")),  # before "now" -- fine for P10
        Percentile(percentile=0.50, value=_ts("2026-12-01")),  # after "now" -- required for median
        Percentile(percentile=0.90, value=_ts("2027-05-01")),
    ]
    dist = _date_distribution(percentiles)
    reason = check_numeric_or_date_distribution(dist, UKRAINE_DATE_QUESTION, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert reason is None
