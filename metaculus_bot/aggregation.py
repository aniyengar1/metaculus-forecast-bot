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

import statistics

import numpy as np
from scipy.interpolate import PchipInterpolator

from forecasting_tools import (
    DateQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

from metaculus_bot.calibration import logit, sigmoid

_MIN_PERCENTILE_STEP = 6e-5


def aggregate_binary_log_odds(probabilities: list[float]) -> float:
    logits = [logit(p) for p in probabilities]
    median_logit = statistics.median(logits)
    return sigmoid(median_logit)


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
    """
    elicited_sorted = sorted(elicited_percentiles, key=lambda p: p.percentile)
    xs = _dedupe_strictly_increasing([p.value for p in elicited_sorted])
    ys = [p.percentile for p in elicited_sorted]

    # SDK-correct x-axis grid (bound/zero-point aware), independent of
    # interpolation method -- get it for free from a scaffold distribution
    # built the default (linear) way, we only use its x-values.
    scaffold = NumericDistribution.from_question(elicited_sorted, question)
    grid_xs = [p.value for p in scaffold.get_cdf()]

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
