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

import pytest
from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile, PredictedOption, PredictedOptionList
from forecasting_tools.data_models.numeric_report import NumericReport
from scipy.interpolate import PchipInterpolator

from metaculus_bot.aggregation import (
    blend_numeric_distributions,
    build_pchip_numeric_distribution,
    build_statistical_numeric_distribution,
    floor_and_renormalize_mc,
)
from metaculus_bot.data_index import DataPoint


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


GAS_PRICE_WITH_HORIZON = GAS_PRICE_NUMERIC_QUESTION.model_copy(
    update={"scheduled_resolution_time": datetime(2026, 12, 1, tzinfo=timezone.utc)}
)
_NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)  # 30 days before the horizon above


def _price_data_point(numeric_value: float, volatility: float) -> DataPoint:
    return DataPoint(
        label="fake price series",
        value=f"${numeric_value:.3f}",
        as_of="2026-11-01",
        source_url="http://example.com",
        numeric_value=numeric_value,
        volatility=volatility,
        period_days=7.0,
        is_price=True,
    )


class TestBuildStatisticalNumericDistribution:
    def test_returns_none_without_numeric_fields(self):
        bare_point = DataPoint(label="x", value="x", as_of="x", source_url="http://example.com")
        assert build_statistical_numeric_distribution(bare_point, GAS_PRICE_WITH_HORIZON, now=_NOW) is None

    def test_median_matches_last_value_for_a_price_series(self):
        point = _price_data_point(4.2, volatility=0.02)
        dist = build_statistical_numeric_distribution(point, GAS_PRICE_WITH_HORIZON, now=_NOW)
        median = dist.get_percentiles_at_target_heights([0.5])[0].value
        assert median == pytest.approx(4.2, rel=1e-2)

    def test_band_widens_with_more_volatility(self):
        tight = build_statistical_numeric_distribution(_price_data_point(4.2, 0.01), GAS_PRICE_WITH_HORIZON, now=_NOW)
        wide = build_statistical_numeric_distribution(_price_data_point(4.2, 0.08), GAS_PRICE_WITH_HORIZON, now=_NOW)
        t10, t90 = (p.value for p in tight.get_percentiles_at_target_heights([0.1, 0.9]))
        w10, w90 = (p.value for p in wide.get_percentiles_at_target_heights([0.1, 0.9]))
        assert (w90 - w10) > (t90 - t10)

    def test_stays_within_question_bounds(self):
        # A volatility large enough that the raw log-normal P10 (~3.3) falls
        # below the question's lower bound (3.5) must clip to the bound
        # rather than overshoot it.
        point = _price_data_point(4.2, volatility=0.09)
        dist = build_statistical_numeric_distribution(point, GAS_PRICE_WITH_HORIZON, now=_NOW)
        p10, p90 = (p.value for p in dist.get_percentiles_at_target_heights([0.1, 0.9]))
        assert p10 == pytest.approx(GAS_PRICE_NUMERIC_QUESTION.lower_bound, abs=1e-3)
        assert p90 <= GAS_PRICE_NUMERIC_QUESTION.upper_bound + 1e-6

    def test_extreme_volatility_falls_back_to_none_instead_of_crashing(self):
        # Volatility this large pushes several adjacent percentile heights
        # to the exact same clipped bound value, which produces a CDF too
        # concentrated for Metaculus's own validation to accept -- no real
        # series this module tracks is anywhere near this volatile, but the
        # function must degrade to "use LLM-only" rather than blow up a
        # question that would otherwise forecast fine.
        point = _price_data_point(4.2, volatility=5.0)
        assert build_statistical_numeric_distribution(point, GAS_PRICE_WITH_HORIZON, now=_NOW) is None

    def test_additive_index_series_can_go_negative(self):
        index_point = DataPoint(
            label="fake index",
            value="-0.5",
            as_of="2026-11-01",
            source_url="http://example.com",
            numeric_value=-0.5,
            volatility=0.3,
            period_days=30.44,
            is_price=False,
        )
        question = NumericQuestion(
            id_of_question=99,
            id_of_post=99,
            question_text="fake index question",
            page_url="https://www.metaculus.com/questions/99",
            upper_bound=3.0,
            lower_bound=-3.0,
            open_upper_bound=True,
            open_lower_bound=True,
            zero_point=None,
            scheduled_resolution_time=datetime(2026, 12, 1, tzinfo=timezone.utc),
        )
        dist = build_statistical_numeric_distribution(index_point, question, now=_NOW)
        median = dist.get_percentiles_at_target_heights([0.5])[0].value
        assert median == pytest.approx(-0.5, abs=0.2)


class TestBlendNumericDistributions:
    def test_blend_lands_between_the_two_inputs(self):
        stats_dist = build_statistical_numeric_distribution(
            _price_data_point(5.0, 0.02), GAS_PRICE_WITH_HORIZON, now=_NOW
        )
        llm_dist = build_pchip_numeric_distribution(
            [
                Percentile(percentile=0.1, value=3.6),
                Percentile(percentile=0.5, value=3.8),
                Percentile(percentile=0.9, value=4.0),
            ],
            GAS_PRICE_NUMERIC_QUESTION,
        )
        blended = blend_numeric_distributions(stats_dist, llm_dist, GAS_PRICE_NUMERIC_QUESTION, stats_weight=0.7)
        blended_median = blended.get_percentiles_at_target_heights([0.5])[0].value
        stats_median = stats_dist.get_percentiles_at_target_heights([0.5])[0].value
        llm_median = llm_dist.get_percentiles_at_target_heights([0.5])[0].value
        lo, hi = sorted((stats_median, llm_median))
        assert lo <= blended_median <= hi

    def test_higher_stats_weight_pulls_closer_to_stats(self):
        stats_dist = build_statistical_numeric_distribution(
            _price_data_point(5.0, 0.02), GAS_PRICE_WITH_HORIZON, now=_NOW
        )
        llm_dist = build_pchip_numeric_distribution(
            [
                Percentile(percentile=0.1, value=3.6),
                Percentile(percentile=0.5, value=3.8),
                Percentile(percentile=0.9, value=4.0),
            ],
            GAS_PRICE_NUMERIC_QUESTION,
        )
        blended_70 = blend_numeric_distributions(stats_dist, llm_dist, GAS_PRICE_NUMERIC_QUESTION, stats_weight=0.7)
        blended_30 = blend_numeric_distributions(stats_dist, llm_dist, GAS_PRICE_NUMERIC_QUESTION, stats_weight=0.3)
        stats_median = stats_dist.get_percentiles_at_target_heights([0.5])[0].value
        median_70 = blended_70.get_percentiles_at_target_heights([0.5])[0].value
        median_30 = blended_30.get_percentiles_at_target_heights([0.5])[0].value
        assert abs(median_70 - stats_median) < abs(median_30 - stats_median)


class TestFloorAndRenormalizeMc:
    def test_floors_near_zero_option_and_still_sums_to_one(self):
        options = PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name="A", probability=0.97),
                PredictedOption(option_name="B", probability=0.02),
                PredictedOption(option_name="C", probability=0.01),
            ]
        )
        floored = floor_and_renormalize_mc(options)
        probs = {o.option_name: o.probability for o in floored.predicted_options}
        assert abs(sum(probs.values()) - 1.0) < 1e-9
        assert probs["C"] >= 0.02 - 1e-9
        assert probs["A"] < 0.97  # renormalization pulls the big option down a little

    def test_no_op_when_already_above_floor(self):
        options = PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name="A", probability=0.6),
                PredictedOption(option_name="B", probability=0.4),
            ]
        )
        floored = floor_and_renormalize_mc(options)
        probs = {o.option_name: o.probability for o in floored.predicted_options}
        assert abs(probs["A"] - 0.6) < 1e-9
        assert abs(probs["B"] - 0.4) < 1e-9

    def test_many_options_each_floored_still_sums_to_one(self):
        # 10 options, one dominant -- every other option under the 2% floor
        # before flooring (a realistic MC shape, e.g. "who will win" with a
        # heavy favorite and many long-shot others).
        options = PredictedOptionList(
            predicted_options=[PredictedOption(option_name="FAVORITE", probability=0.91)]
            + [PredictedOption(option_name=f"LONGSHOT_{i}", probability=0.01) for i in range(9)]
        )
        floored = floor_and_renormalize_mc(options)
        probs = {o.option_name: o.probability for o in floored.predicted_options}
        assert abs(sum(probs.values()) - 1.0) < 1e-9
        for i in range(9):
            assert probs[f"LONGSHOT_{i}"] >= 0.02 - 1e-9
