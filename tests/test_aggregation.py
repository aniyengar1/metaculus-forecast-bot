"""
Regression tests for a real incident (2026-10-16): a date question's
submitted median came out to 2024-11-26, before the question even opened
(2026-04-27). Root cause: build_pchip_numeric_distribution fit PCHIP only
to each model's own sparse elicited percentiles (e.g. 6 points spanning
just the model's believed P10-P90 window), then evaluated that fit at a
grid spanning the question's FULL [lower_bound, upper_bound] range -- often
years wider than the model's own elicited window. PchipInterpolator's cubic
fit is only well-behaved inside its fitted domain; extrapolated far outside
it (extrapolate=True), it can swing wildly (reproduced below: raw values of
600%+ "probability"). np.clip(0,1) clamps that to 1.0, and
np.maximum.accumulate then locks that 1.0 in as a floor for the entire rest
of the (chronologically later) curve, corrupting the median.

Fix: extend the elicited percentiles with explicit bound anchors (via the
SDK's own NumericDistribution._add_explicit_upper_lower_bound_percentiles --
the same helper the SDK's own default linear-CDF builder relies on) before
fitting PCHIP, so the fit's domain already covers the full grid and little
or no extrapolation is needed.

Uses the real Ukraine martial-law question's actual bounds/settings
(post 43324, pulled from the live API 2026-10-16) and the real gas-price
numeric question's bounds (post 45960), plus the real per-model elicited
percentiles that were actually submitted for 43324 (from
raw_model_outputs in the production forecast log), to reproduce the exact
incident -- not just a synthetic approximation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile
from forecasting_tools.data_models.numeric_report import NumericReport
from scipy.interpolate import PchipInterpolator

from metaculus_bot.aggregation import build_pchip_numeric_distribution


def _ts(date_str: str) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


# Real bounds for post 43324 ("When will martial law be lifted in at least
# 3/4 of Ukraine?"), pulled from the live API 2026-10-16.
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

# Real bounds for post 45960 ("What will the United States national
# average gas price be..."), pulled from the live API 2026-10-16.
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

# The two real models' actual elicited percentiles submitted for 43324
# (from raw_model_outputs in the production DB, run_timestamp
# 2026-10-09T00:44:11Z) -- both clustered within a span of well under two
# years, far narrower than the question's ~5-year full range.
_REAL_MODEL_1_PERCENTILES = [
    Percentile(percentile=0.10, value=_ts("2027-02-01")),
    Percentile(percentile=0.20, value=_ts("2027-05-01")),
    Percentile(percentile=0.40, value=_ts("2027-08-01")),
    Percentile(percentile=0.60, value=_ts("2027-11-01")),
    Percentile(percentile=0.80, value=_ts("2028-02-01")),
    Percentile(percentile=0.90, value=_ts("2028-05-01")),
]
_REAL_MODEL_2_PERCENTILES = [
    Percentile(percentile=0.10, value=_ts("2026-11-01")),
    Percentile(percentile=0.20, value=_ts("2027-01-15")),
    Percentile(percentile=0.40, value=_ts("2027-03-15")),
    Percentile(percentile=0.60, value=_ts("2027-04-15")),
    Percentile(percentile=0.80, value=_ts("2027-05-15")),
    Percentile(percentile=0.90, value=_ts("2027-05-28")),
]


def test_build_pchip_numeric_distribution_adds_bound_anchors_before_fitting():
    """The fix itself: the xs actually fit by PCHIP must span the full
    [lower_bound, upper_bound] range, not just the model's own elicited
    window -- otherwise grid points outside that window require
    extrapolation (the bug)."""
    dist = build_pchip_numeric_distribution(_REAL_MODEL_2_PERCENTILES, UKRAINE_DATE_QUESTION)
    values = [p.value for p in dist.declared_percentiles]
    assert min(values) <= UKRAINE_DATE_QUESTION.lower_bound.timestamp() + 1
    assert max(values) >= UKRAINE_DATE_QUESTION.upper_bound.timestamp() - 1


def test_single_model_date_distribution_has_no_overshoot_before_its_own_elicited_range():
    """Reproduces the raw overshoot directly: fitting PCHIP only to the
    narrow elicited window and evaluating across the full 5-year question
    range produces raw values over 600% probability at the far (pre-2026)
    end of the grid -- this test fails on the pre-fix code."""
    scaffold = NumericDistribution.from_question(
        sorted(_REAL_MODEL_2_PERCENTILES, key=lambda p: p.percentile), UKRAINE_DATE_QUESTION
    )
    grid_xs = [p.value for p in scaffold.get_cdf()]

    # Unfixed: PCHIP fit directly to the narrow elicited window only.
    xs_narrow = [p.value for p in _REAL_MODEL_2_PERCENTILES]
    ys_narrow = [p.percentile for p in _REAL_MODEL_2_PERCENTILES]
    pchip_unfixed = PchipInterpolator(xs_narrow, ys_narrow, extrapolate=True)
    raw_unfixed = pchip_unfixed(grid_xs)
    assert max(raw_unfixed) > 2.0  # the actual overshoot this bug produces

    # Fixed: our function's internal bound-anchored fit should never
    # produce a raw probability far outside [0, 1] before clipping.
    dist = build_pchip_numeric_distribution(_REAL_MODEL_2_PERCENTILES, UKRAINE_DATE_QUESTION)
    percentiles_before_2026 = [p for p in dist.declared_percentiles if p.value < _ts("2026-01-01")]
    assert all(p.percentile < 0.15 for p in percentiles_before_2026)


def test_date_question_aggregate_median_is_after_question_open_time():
    """The actual incident: using the real submitted per-model percentiles
    for 43324, the aggregate median must land after the question opened
    (2026-04-27) and, for this specific real data, well into 2026-2027 --
    not 2024."""
    dist1 = build_pchip_numeric_distribution(_REAL_MODEL_1_PERCENTILES, UKRAINE_DATE_QUESTION)
    dist2 = build_pchip_numeric_distribution(_REAL_MODEL_2_PERCENTILES, UKRAINE_DATE_QUESTION)

    aggregate = asyncio.run(NumericReport.aggregate_predictions([dist1, dist2], UKRAINE_DATE_QUESTION))
    median = aggregate.get_percentiles_at_target_heights([0.5])[0].value
    median_date = datetime.fromtimestamp(median, tz=timezone.utc)

    question_open_time = datetime(2026, 4, 27, tzinfo=timezone.utc)
    assert median_date > question_open_time
    assert median_date.year in (2026, 2027)  # matches both models' actual elicited windows


def test_numeric_question_distribution_stays_within_bounds_envelope():
    """Same bug class applies to NumericQuestion (shared function) -- use
    the real gas-price question's bounds with a model confident in a
    narrow sub-range of the full [3.5, 6.0] range."""
    narrow_percentiles = [
        Percentile(percentile=0.10, value=4.0),
        Percentile(percentile=0.20, value=4.1),
        Percentile(percentile=0.40, value=4.2),
        Percentile(percentile=0.60, value=4.3),
        Percentile(percentile=0.80, value=4.4),
        Percentile(percentile=0.90, value=4.5),
    ]
    dist = build_pchip_numeric_distribution(narrow_percentiles, GAS_PRICE_NUMERIC_QUESTION)
    median = dist.get_percentiles_at_target_heights([0.5])[0].value
    # A sane median should stay close to the model's own elicited window,
    # not get dragged toward either extreme of the full [3.5, 6.0] range by
    # extrapolation overshoot.
    assert 3.9 <= median <= 4.6


def test_declared_percentiles_are_monotonically_increasing():
    dist = build_pchip_numeric_distribution(_REAL_MODEL_2_PERCENTILES, UKRAINE_DATE_QUESTION)
    percentiles = [p.percentile for p in dist.declared_percentiles]
    assert all(b >= a for a, b in zip(percentiles, percentiles[1:]))
    values = [p.value for p in dist.declared_percentiles]
    assert all(b >= a for a, b in zip(values, values[1:]))
