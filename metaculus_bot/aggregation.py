"""
Custom ensemble aggregation, replacing forecasting_tools' defaults where the
Phase 1 research (nostreambot, Panshul42) showed a better approach:

- Binary: median in log-odds space (default SDK behavior is plain median of
  probabilities; log-odds median is less sensitive to a single model's
  overconfident near-0/near-1 outlier).
- Multiple choice: per-option MEDIAN across models, then renormalize (default
  SDK behavior is per-option MEAN).
- Numeric/date: each model's elicited percentiles are turned into a full CDF
  via PCHIP (monotone cubic Hermite) interpolation instead of the SDK's
  default piecewise-linear interpolation, which produces jagged CDFs between
  sparse elicited points. Final pointwise-median-of-CDFs aggregation across
  models is left to forecasting_tools.NumericReport.aggregate_predictions,
  which already does exactly that.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.stats import norm

from forecasting_tools import (
    DateQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

from metaculus_bot.calibration import logit, sigmoid
from metaculus_bot.data_index import DataPoint

logger = logging.getLogger(__name__)

_MIN_PERCENTILE_STEP = 6e-5

# Representative percentile heights used by both the statistical-blend
# functions below and research_quality.widen_numeric_distribution -- the
# same spread (P10/20/40/50/60/80/90) every sparse-percentile-manipulation
# helper in this codebase builds on.
_BLEND_HEIGHTS = (0.1, 0.2, 0.4, 0.5, 0.6, 0.8, 0.9)


def aggregate_binary_log_odds(probabilities: list[float]) -> float:
    logits = [logit(p) for p in probabilities]
    median_logit = statistics.median(logits)
    return sigmoid(median_logit)


# How fast a model's weight grows into its own inverse-Brier value as its
# resolved-question count n grows -- shrink = n/(n+K), so n==K is exactly
# halfway. 30 chosen so a model needs real history (dozens of resolved
# questions) before its own track record dominates; below ~15 resolved
# questions it stays close to equal weight regardless of Brier.
DEFAULT_WEIGHT_SHRINK_K = 30.0


def shrunk_inverse_brier_weight(brier: float, n: int, k_shrink: float = DEFAULT_WEIGHT_SHRINK_K) -> float:
    """Inverse-Brier weight (1/brier), shrunk toward the equal-weight
    baseline of 1.0 by shrink = n/(n+k_shrink): n=0 gives exactly 1.0
    (fully equal), n >> k_shrink converges to 1/brier (fully earned).
    `brier` must be > 0 (a real Brier score on resolved binaries always is,
    short of a model that's never anything but perfectly wrong)."""
    shrink = n / (n + k_shrink)
    return shrink / brier + (1 - shrink)


def aggregate_binary_log_odds_weighted(
    model_values: list[tuple[str, float]], weights: dict[str, float]
) -> float:
    """Weighted mean in log-odds space. Weights are normalized to sum to 1
    among just the models actually present in this run -- so a run that
    fell back to an OpenRouter model for one slot still produces a valid,
    fully-weighted aggregate, not a partial one. A model missing from
    `weights` (never resolved/scored yet) defaults to 1.0, the same value
    shrunk_inverse_brier_weight converges to at n=0."""
    raw_weights = [weights.get(model, 1.0) for model, _ in model_values]
    total_weight = sum(raw_weights)
    logits = [logit(value) for _, value in model_values]
    weighted_logit = sum(w * lg for w, lg in zip(raw_weights, logits)) / total_weight
    return sigmoid(weighted_logit)


def aggregate_multiple_choice_median(
    option_lists: list[PredictedOptionList],
) -> PredictedOptionList:
    first_names = [o.option_name for o in option_lists[0].predicted_options]
    for ol in option_lists:
        names = {o.option_name for o in ol.predicted_options}
        if names != set(first_names):
            raise ValueError(
                f"All predictions must have the same option names, but {names} != {set(first_names)}"
            )

    medians: list[float] = []
    for name in first_names:
        values = [
            o.probability
            for ol in option_lists
            for o in ol.predicted_options
            if o.option_name == name
        ]
        medians.append(statistics.median(values))

    total = sum(medians)
    renormalized = [m / total for m in medians]
    return PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name=name, probability=p)
            for name, p in zip(first_names, renormalized)
        ]
    )


_MC_OPTION_FLOOR = 0.02


def floor_and_renormalize_mc(
    option_list: PredictedOptionList, floor: float = _MC_OPTION_FLOOR
) -> PredictedOptionList:
    """Floors every option's probability at `floor` (2% by default) and
    renormalizes so the list still sums to 1 -- a multiple-choice ensemble
    that's confidently near 0% on an option is making the same kind of
    overconfident bet the binary calibration clip (see
    metaculus_bot.calibration/research_quality) now guards against, just
    with more than two outcomes.

    A single floor-then-divide-by-new-total pass isn't enough: dividing by
    a total inflated by the floored entries can push a *different* entry
    back under the floor (e.g. [0.97, 0.02, 0.01] with floor 0.02 floors
    index 2 to 0.02, but the new total 1.01 then rescales index 1 down to
    0.0198 -- under the floor again). Water-fill instead: lock any entry
    under the floor at exactly `floor`, rescale only the remaining ("free")
    entries to absorb 1 minus the locked mass, and repeat until a pass locks
    nothing new -- at most len(options) passes."""
    options = option_list.predicted_options
    original = [o.probability for o in options]
    n = len(original)
    locked = [False] * n
    result = list(original)

    for _ in range(n):
        free_indices = [i for i in range(n) if not locked[i]]
        if not free_indices:
            break
        remaining = 1.0 - sum(floor for i in range(n) if locked[i])
        free_total = sum(original[i] for i in free_indices)
        if free_total <= 0:
            share = remaining / len(free_indices)
            for i in free_indices:
                result[i] = share
            break
        scale = remaining / free_total
        newly_locked = False
        for i in free_indices:
            scaled = original[i] * scale
            if scaled < floor:
                result[i] = floor
                locked[i] = True
                newly_locked = True
            else:
                result[i] = scaled
        if not newly_locked:
            break

    return PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name=o.option_name, probability=p)
            for o, p in zip(options, result)
        ]
    )


def _dedupe_strictly_increasing(xs: list[float]) -> list[float]:
    out = list(xs)
    for i in range(1, len(out)):
        if out[i] <= out[i - 1]:
            out[i] = out[i - 1] + 1e-9 * max(abs(out[i - 1]), 1.0)
    return out


def _enforce_min_step_increasing(ys: list[float]) -> list[float]:
    """Walk forward enforcing a minimum step, then re-walk backward from the
    end if that pushed anything above 1.0, to keep the whole curve in [0, 1]
    while staying strictly increasing."""
    out = list(ys)
    for i in range(1, len(out)):
        min_allowed = out[i - 1] + _MIN_PERCENTILE_STEP
        if out[i] < min_allowed:
            out[i] = min_allowed
    if out[-1] > 1.0:
        out[-1] = 1.0
        for i in range(len(out) - 2, -1, -1):
            max_allowed = out[i + 1] - _MIN_PERCENTILE_STEP
            if out[i] > max_allowed:
                out[i] = max_allowed
            else:
                break
    return out


def build_pchip_numeric_distribution(
    elicited_percentiles: list[Percentile],
    question: NumericQuestion | DateQuestion,
) -> NumericDistribution:
    """
    Builds a full NumericDistribution/CDF from a sparse set of elicited
    percentiles (e.g. P10/20/40/60/80/90) using PCHIP interpolation, then
    re-validates/standardizes through the SDK's own NumericDistribution so
    Metaculus's CDF validity constraints (bound pinning, min/max step per
    bin, etc) are still enforced.

    Before fitting, extends the elicited percentiles with explicit anchor
    points at the question's lower/upper bound via the SDK's own
    _add_explicit_upper_lower_bound_percentiles -- the same private helper
    NumericDistribution's own default (linear) CDF builder uses internally
    (see get_cdf -> _get_cdf_at in forecasting_tools). Without this, PCHIP
    has to extrapolate its cubic fit far outside the model's own elicited
    range to cover the full [lower_bound, upper_bound] grid every question
    needs -- a model confident the answer lands in a narrow window (e.g. 6
    months of a question whose range spans 5 years) leaves PCHIP
    extrapolating across years of empty domain. Confirmed empirically this
    can overshoot to 600%+ raw probability, and the np.clip + a few lines
    down then locks that overshoot in as a floor for
    the entire rest of the curve via np.maximum.accumulate. This was the
    root cause of a real incident (2026-10-16): a date question's submitted
    median came out before the question even opened. See
    docs/STAGE_B_PLAN.md "Date/numeric aggregation bug" and
    tests/test_aggregation.py for the reproduction. (Reaches into a private
    SDK method rather than reimplementing the same bound-anchoring
    convention ourselves, to avoid silently drifting from Metaculus's own
    rules -- re-verify this still exists if forecasting_tools' pinned
    version range in pyproject.toml ever changes.)
    """
    elicited_sorted = sorted(elicited_percentiles, key=lambda p: p.percentile)

    # SDK-correct x-axis grid (bound/zero-point aware), independent of
    # interpolation method -- get it for free from a scaffold distribution
    # built the default (linear) way, we only use its x-values.
    scaffold = NumericDistribution.from_question(elicited_sorted, question)
    grid_xs = [p.value for p in scaffold.get_cdf()]

    bounded_percentiles = scaffold._add_explicit_upper_lower_bound_percentiles(elicited_sorted)
    xs = _dedupe_strictly_increasing([p.value for p in bounded_percentiles])
    ys = [p.percentile for p in bounded_percentiles]

    pchip = PchipInterpolator(xs, ys, extrapolate=True)
    raw_ys = pchip(grid_xs)
    clipped = np.clip(raw_ys, 0.0, 1.0)
    monotone = np.maximum.accumulate(clipped).tolist()
    monotone = _enforce_min_step_increasing(monotone)

    dense_percentiles = [
        Percentile(value=x, percentile=y) for x, y in zip(grid_xs, monotone)
    ]

    final = NumericDistribution.from_question(
        dense_percentiles, question, standardize_cdf=True
    )
    return final


def build_statistical_numeric_distribution(
    data_point: DataPoint, question: NumericQuestion, now: datetime | None = None
) -> NumericDistribution | None:
    """Builds a numeric distribution purely from a tracked data series'
    last value and empirical volatility (metaculus_bot.data_index), with no
    LLM call involved: a random-walk model scaled to the time left until
    resolution via sqrt(time) -- log-normal (volatility = stdev of log
    returns) for a price-like series, since prices can't go negative and
    multiplicative moves are the standard model; additive/normal
    (volatility = stdev of raw differences) otherwise, since an index like
    ONI's anomaly can go negative.

    Returns None if `data_point` doesn't carry the numeric fields needed
    (older/display-only DataPoints, or a series whose fetch() couldn't
    compute a volatility) -- the caller falls back to LLM-only, same as a
    fetch failure already does one layer up in data_index.fetch_direct_data."""
    if data_point.numeric_value is None or data_point.volatility is None or not data_point.period_days:
        return None

    now = now or datetime.now(timezone.utc)
    resolution_time = question.scheduled_resolution_time or question.close_time
    horizon_days = (resolution_time - now).total_seconds() / 86400.0 if resolution_time else 1.0
    horizon_days = max(horizon_days, 1.0)  # at least one day out, never shrink/zero the spread
    periods_ahead = horizon_days / data_point.period_days
    sigma_h = data_point.volatility * math.sqrt(periods_ahead)

    percentiles: list[Percentile] = []
    for height in _BLEND_HEIGHTS:
        z = norm.ppf(height)
        if data_point.is_price:
            value = data_point.numeric_value * math.exp(z * sigma_h)
        else:
            value = data_point.numeric_value + z * sigma_h
        value = min(max(value, question.lower_bound), question.upper_bound)
        percentiles.append(Percentile(percentile=height, value=value))

    deduped_values = _dedupe_strictly_increasing([p.value for p in percentiles])
    percentiles = [Percentile(percentile=p.percentile, value=v) for p, v in zip(percentiles, deduped_values)]
    try:
        return build_pchip_numeric_distribution(percentiles, question)
    except Exception as e:
        # An extreme enough volatility/horizon combination can push several
        # adjacent percentile heights to the same clipped bound value (e.g.
        # both P10 and P20 clip to the lower bound), which _dedupe's tiny
        # epsilon nudge isn't enough to satisfy Metaculus's own "not too
        # concentrated" validation on the rebuilt CDF. No real series this
        # module tracks is anywhere near volatile enough to actually hit
        # this, but falling back to LLM-only on it is far better than
        # crashing a question that would otherwise forecast fine.
        logger.warning(f"Statistical numeric distribution build failed, falling back to LLM-only: {e}")
        return None


def blend_numeric_distributions(
    stats_distribution: NumericDistribution,
    llm_distribution: NumericDistribution,
    question: NumericQuestion,
    stats_weight: float = 0.7,
) -> NumericDistribution:
    """Blends a statistical (data-series) distribution with the LLM
    ensemble's own distribution by taking a weighted average of each
    representative percentile's VALUE (same technique
    research_quality.widen_numeric_distribution already uses to manipulate
    a distribution: pull a few representative percentiles, transform,
    rebuild via PCHIP) -- 70% stats / 30% LLM by default, on the premise
    that a tracked series' last value plus its own historical volatility is
    a stronger anchor than an LLM's elicited percentiles, but the LLM
    component still lets qualitative research (an announced policy change,
    a known seasonal effect) pull the forecast away from a pure random walk."""
    stats_reps = stats_distribution.get_percentiles_at_target_heights(list(_BLEND_HEIGHTS))
    llm_reps = llm_distribution.get_percentiles_at_target_heights(list(_BLEND_HEIGHTS))

    blended_values = [
        stats_weight * s.value + (1 - stats_weight) * l.value for s, l in zip(stats_reps, llm_reps)
    ]
    deduped_values = _dedupe_strictly_increasing(blended_values)
    blended_percentiles = [
        Percentile(percentile=s.percentile, value=v) for s, v in zip(stats_reps, deduped_values)
    ]
    return build_pchip_numeric_distribution(blended_percentiles, question)
