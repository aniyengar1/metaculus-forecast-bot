"""
Deterministic (regex-based) parsing of each model's raw forecast text, tried
before falling back to an LLM parser call. Our prompts (bot.py) all specify
an exact output format, so most responses parse without spending a single
extra OpenRouter request -- the whole point, since free-tier accounts are
tightly rate-limited (see docs/STAGE_B_PLAN.md). Every function here returns
None on any ambiguity/failure rather than guessing, so the caller can fall
back to the LLM parser (which is far more tolerant of malformed text).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from forecasting_tools import DatePercentile, Percentile, PredictedOption, PredictedOptionList

_BINARY_RE = re.compile(r"Probability:\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_PERCENTILE_NUMERIC_RE = re.compile(
    r"Percentile\s*(\d+)\s*:\s*(-?[\d,]+(?:\.\d+)?)", re.IGNORECASE
)
_PERCENTILE_DATE_RE = re.compile(
    r"Percentile\s*(\d+)\s*:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE
)


def parse_binary(text: str) -> float | None:
    """Looks for the last 'Probability: ZZ%' in the text (our binary prompt's
    specified format). Returns a decimal in [0.01, 0.99], or None if no such
    line is found."""
    matches = _BINARY_RE.findall(text)
    if not matches:
        return None
    try:
        value = float(matches[-1]) / 100.0
    except ValueError:
        return None
    return max(0.01, min(0.99, value))


def parse_multiple_choice(text: str, options: list[str]) -> PredictedOptionList | None:
    """Looks for one 'OptionName: NN[%]' line per option (our MC prompt's
    format). All options must be found and parseable, and the raw numbers
    must be consistently scaled (all ~0-1 decimals or all ~0-100
    percentages) -- a mix is treated as ambiguous. Renormalization to sum=1
    is handled by PredictedOptionList itself."""
    raw_values: list[float] = []
    for option in options:
        pattern = re.compile(
            rf"{re.escape(option)}\s*:\s*(\d+(?:\.\d+)?)\s*%?", re.IGNORECASE
        )
        matches = pattern.findall(text)
        if not matches:
            return None
        try:
            raw_values.append(float(matches[-1]))
        except ValueError:
            return None

    total = sum(raw_values)
    if total <= 0:
        return None
    # Accept either percentage-scale (~100) or decimal-scale (~1) input,
    # consistently across all options; anything else is too ambiguous to
    # trust without an LLM's judgement.
    if not (0.8 <= total <= 1.2 or 80 <= total <= 120):
        return None
    probabilities = [v / total for v in raw_values]

    try:
        return PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name=name, probability=p)
                for name, p in zip(options, probabilities)
            ]
        )
    except Exception:
        return None


def parse_numeric_percentiles(text: str) -> list[Percentile] | None:
    """Looks for 'Percentile NN: VALUE' lines (our numeric prompt's format).
    Requires at least 2 distinct, strictly-increasing (percentile, value)
    points; later repeats of the same percentile number override earlier
    ones (handles a model restating its answer)."""
    by_percentile: dict[float, float] = {}
    for pct_str, val_str in _PERCENTILE_NUMERIC_RE.findall(text):
        try:
            pct = float(pct_str) / 100.0
            value = float(val_str.replace(",", ""))
        except ValueError:
            continue
        if 0 <= pct <= 1:
            by_percentile[pct] = value

    if len(by_percentile) < 2:
        return None

    points = sorted(by_percentile.items())
    for i in range(1, len(points)):
        if points[i][1] <= points[i - 1][1]:
            return None  # not strictly increasing -- let the LLM parser sort it out

    return [Percentile(percentile=pct, value=value) for pct, value in points]


def parse_date_percentiles(text: str) -> list[DatePercentile] | None:
    """Looks for 'Percentile NN: YYYY-MM-DD' lines (our date prompt's
    format), assuming midnight UTC. Same monotonicity requirement as numeric."""
    by_percentile: dict[float, datetime] = {}
    for pct_str, date_str in _PERCENTILE_DATE_RE.findall(text):
        try:
            pct = float(pct_str) / 100.0
            value = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if 0 <= pct <= 1:
            by_percentile[pct] = value

    if len(by_percentile) < 2:
        return None

    points = sorted(by_percentile.items())
    for i in range(1, len(points)):
        if points[i][1] <= points[i - 1][1]:
            return None

    return [DatePercentile(percentile=pct, value=value) for pct, value in points]
