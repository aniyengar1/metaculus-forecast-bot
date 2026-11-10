"""
Research-quality guard: a cheap, deterministic check on the research summary
text (no extra LLM call) that flags "weak" research -- empty, very short, a
known failure phrase from research.py's own fallback chain, or no dated
source anywhere in it -- and pulls the final forecast toward the
uninformative prior when it fires. This does not replace research quality;
it bounds how much damage a confident-but-uninformed ensemble can do on a
question where the research step came back thin (see the 2026-11-09 review:
the worst loss of the season, the El Nino question, had all three models
agree confidently on the wrong side with no dated source backing them up).

Shrink/widen amounts share one `GUARD_FACTOR`: binary/MC move the final
prediction 50% of the way back to the uninformative prior (0.5 / uniform);
numeric/date widen the P10-P90 band by the reciprocal (2x) around the
median. Both directions are deliberately the same magnitude so the guard's
strength is easy to reason about in one place.
"""
from __future__ import annotations

import re

from forecasting_tools import (
    DateQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

from metaculus_bot.aggregation import _dedupe_strictly_increasing, build_pchip_numeric_distribution
from metaculus_bot.calibration import calibrate_binary_probability

GUARD_FACTOR = 0.5
GUARD_NUMERIC_WIDEN_FACTOR = 1.0 / GUARD_FACTOR  # 2.0
GUARD_BINARY_CLIP_MIN = 0.15
GUARD_BINARY_CLIP_MAX = 0.85

# Heuristic floor for "very short" -- a real 200-word LLM summary is
# typically 600-1200 chars; a one-line "nothing found" style response from
# either research fallback is well under this.
_MIN_SUBSTANTIVE_CHARS = 120

_FAILURE_PHRASES = (
    "no recent asknews articles found",
    "no web search results found",
    "no research available",
)

# Any 4-digit year or an explicit YYYY-MM-DD is treated as "has a dated
# source" -- intentionally loose (this is a cheap heuristic, not a citation
# checker): research that never mentions *any* date is the case we actually
# want to catch (e.g. a generic, undated LLM paraphrase with nothing recent
# behind it).
_DATE_PATTERN = re.compile(r"\b(19|20)\d{2}\b|\b\d{4}-\d{2}-\d{2}\b")

_SOURCE_TAG_PATTERN = re.compile(r"^\[research source:[^\]]*\]\s*", re.IGNORECASE)


def assess_research_quality(research_summary: str) -> tuple[str, str]:
    """Returns (quality, reason), quality in {"weak", "ok"}."""
    text = research_summary or ""
    body = _SOURCE_TAG_PATTERN.sub("", text).strip()
    lowered = body.lower()

    if not body:
        return "weak", "research summary is empty"
    if len(body) < _MIN_SUBSTANTIVE_CHARS:
        return "weak", f"research summary is very short ({len(body)} chars)"
    if any(phrase in lowered for phrase in _FAILURE_PHRASES):
        return "weak", "research source reported no results"
    if not _DATE_PATTERN.search(body):
        return "weak", "no dated source found anywhere in the research"
    return "ok", "research has substantive, dated content"


def shrink_binary_toward_half(p: float) -> float:
    """Pulls a binary probability 50% of the way (in log-odds) back toward
    0.5, then clips to [0.15, 0.85] -- reuses the existing calibration hook
    rather than a parallel implementation, since "shrink toward 0.5 in
    log-odds by a factor" is exactly what calibrate_binary_probability(k<1)
    already does."""
    return calibrate_binary_probability(p, GUARD_FACTOR, GUARD_BINARY_CLIP_MIN, GUARD_BINARY_CLIP_MAX)


def flatten_mc_toward_uniform(
    option_list: PredictedOptionList, factor: float = GUARD_FACTOR
) -> PredictedOptionList:
    """Moves every option's probability `factor` of the way toward the
    uniform distribution (1/N each). Already sums to 1 afterward (the
    original probabilities and the uniform ones each sum to 1, and this is
    a convex combination of the two), so no renormalization step is needed."""
    options = option_list.predicted_options
    n = len(options)
    uniform = 1.0 / n
    flattened = [
        PredictedOption(
            option_name=o.option_name,
            probability=factor * o.probability + (1 - factor) * uniform,
        )
        for o in options
    ]
    return PredictedOptionList(predicted_options=flattened)


def widen_numeric_distribution(
    distribution: NumericDistribution,
    question: NumericQuestion | DateQuestion,
    widen_factor: float = GUARD_NUMERIC_WIDEN_FACTOR,
) -> NumericDistribution:
    """Widens the aggregated distribution by pushing a handful of
    representative percentiles away from the median by `widen_factor`
    (clipped to the question's own bounds), then rebuilds a full CDF through
    the same PCHIP pipeline aggregation.py already uses for the initial
    per-model build -- deliberately reused rather than hand-rolled, so this
    distribution satisfies the exact same Metaculus validity constraints."""
    is_date = isinstance(question, DateQuestion)
    lower_bound = question.lower_bound.timestamp() if is_date else question.lower_bound
    upper_bound = question.upper_bound.timestamp() if is_date else question.upper_bound

    heights = [0.1, 0.2, 0.4, 0.5, 0.6, 0.8, 0.9]
    reps = distribution.get_percentiles_at_target_heights(heights)
    median_value = next(p.value for p in reps if abs(p.percentile - 0.5) < 1e-9)

    widened_values = [
        min(max(median_value + (p.value - median_value) * widen_factor, lower_bound), upper_bound)
        for p in reps
    ]
    deduped_values = _dedupe_strictly_increasing(widened_values)
    widened_percentiles = [
        Percentile(percentile=p.percentile, value=v) for p, v in zip(reps, deduped_values)
    ]
    return build_pchip_numeric_distribution(widened_percentiles, question)
