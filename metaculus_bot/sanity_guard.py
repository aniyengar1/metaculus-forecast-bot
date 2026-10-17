"""
Hard pre-submit sanity guard for numeric/date forecasts, run right before
submission. Checks the final aggregated distribution's median and
10th/90th percentiles against the question's own declared bounds, plus
(date questions only) that the median isn't already in the past. See
docs/STAGE_B_PLAN.md "Date/numeric aggregation bug" for the real incident
this guards against: a date question's submitted median computed to a date
before the question even opened, caused by PCHIP extrapolation overshoot
in metaculus_bot/aggregation.py (fixed separately, but this guard exists
as a second, independent line of defense against the whole bug class --
any future aggregation change that produces a similarly nonsensical
distribution should be caught here before it reaches Metaculus, not after).

If a check fails, the caller must NOT submit -- see
FutureEvalBot._check_distribution_or_raise in bot.py, which logs an alert
and a 'skipped_invalid_distribution' forecast row, then skips the question
entirely (mirroring the existing budget-guard skip pattern).
"""
from __future__ import annotations

from datetime import datetime, timezone

from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion

_BOUND_EPSILON_SECONDS = 1.0  # float-rounding slack at the exact bound


def check_numeric_or_date_distribution(
    distribution: NumericDistribution,
    question: NumericQuestion | DateQuestion,
    *,
    now: datetime | None = None,
) -> str | None:
    """Returns None if the distribution passes every check, else a
    human-readable reason it must not be submitted."""
    now = now or datetime.now(timezone.utc)
    is_date = isinstance(question, DateQuestion)
    lower_bound = question.lower_bound.timestamp() if is_date else question.lower_bound
    upper_bound = question.upper_bound.timestamp() if is_date else question.upper_bound

    percentiles = distribution.get_percentiles_at_target_heights([0.1, 0.5, 0.9])
    p10, median, p90 = (p.value for p in percentiles)

    # The question's value axis (what get_percentiles_at_target_heights
    # returns) is always within [lower_bound, upper_bound] by construction
    # in a well-formed NumericDistribution -- open/closed bounds affect
    # where *probability mass* can sit (e.g. cdf[0] > 0 for an open lower
    # bound), not the value-axis range itself. So this check should always
    # pass for correctly-built distributions; it's a backstop against a
    # future aggregation bug producing an out-of-range value directly,
    # not something that needs different thresholds per open/closed.
    for label, value in [("P10", p10), ("median", median), ("P90", p90)]:
        if value < lower_bound - _BOUND_EPSILON_SECONDS or value > upper_bound + _BOUND_EPSILON_SECONDS:
            return (
                f"{label} ({_format(value, is_date)}) is outside the question's range "
                f"[{_format(lower_bound, is_date)}, {_format(upper_bound, is_date)}]"
            )

    if is_date:
        median_dt = datetime.fromtimestamp(median, tz=timezone.utc)
        if median_dt <= now:
            return f"median resolution date ({median_dt.date()}) is not after now ({now.date()})"

    return None


def _format(value: float, is_date: bool) -> str:
    if is_date:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{value:.4g}"
