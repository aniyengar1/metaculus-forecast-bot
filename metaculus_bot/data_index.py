"""
Direct-fetch official data for questions about well-known published series,
instead of leaving a government-published number to LLM/news-search research
to (mis)summarize. Registry pattern (`_SERIES`) so adding a new series (CPI,
unemployment, ...) is one more `DataSeries` entry plus a fetch function --
nothing else in the pipeline needs to change.

Every fetcher here hits a public, free, official source and is read-only
(GET only, never writes/posts anything). If a fetch fails (network error,
schema change, etc.) this returns None and the caller falls back to whatever
research.gather_research already produced -- the research_quality guard
(metaculus_bot/research_quality.py) then judges that fallback on its own
merits, so a fetch failure degrades to "maybe weak research", not a crash.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
from dataclasses import dataclass
from typing import Callable

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15

# Lower bound on how many period-over-period changes we'll trust a stdev
# estimate from -- below this, treat volatility as unknown (None) rather
# than report a number computed from 1-2 noisy data points.
_MIN_CHANGES_FOR_VOLATILITY = 8


@dataclass
class DataPoint:
    label: str
    value: str
    as_of: str
    source_url: str
    # The fields below are optional and only populated by series that also
    # support metaculus_bot.aggregation.build_statistical_numeric_distribution
    # (currently oni_enso and eia_gas_price) -- a series whose fetch()
    # leaves these as None simply can't be used for the statistical blend,
    # and the numeric forecast path falls back to LLM-only, same as a
    # series fetch failure already does.
    numeric_value: float | None = None
    # Empirical stdev of period-over-period change -- raw difference for
    # an additive/normal series (is_price=False), log-return for a
    # multiplicative/log-normal one (is_price=True).
    volatility: float | None = None
    # Sampling interval the volatility above was computed over, in days
    # (e.g. 7 for a weekly series, ~30.44 for monthly) -- needed to scale
    # volatility to an arbitrary forecast horizon via sqrt(time).
    period_days: float | None = None
    is_price: bool = False

    def as_research_block(self) -> str:
        return f"[Direct data: {self.label}, as of {self.as_of} -- {self.value} (source: {self.source_url})]"


@dataclass
class DataSeries:
    name: str
    # Case-insensitive substrings; ANY match against the question text +
    # resolution criteria triggers this series. Deliberately broad (a false
    # positive just means a question gets an extra, correct, on-topic data
    # point it didn't strictly need -- a false negative means no help at
    # all), so keep adding synonyms here rather than narrowing.
    keywords: tuple[str, ...]
    fetch: Callable[[], DataPoint]


def _fetch_oni() -> DataPoint:
    """NOAA CPC's Oceanic Nino Index -- the standard ENSO (El Nino/La Nina)
    strength measure, a 3-month running mean SST anomaly in the Nino 3.4
    region. Published monthly, free, no API key. Plain-text table, most
    recent period is the last non-empty line.

    Volatility: stdev of month-over-month changes in the anomaly, computed
    from the same response (the file is the full history back to 1950, one
    request either way) -- an additive/normal series, not log-normal (the
    anomaly can go negative), one sample per ~30.44 days."""
    resp = requests.get(
        "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt", timeout=_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    lines = [line for line in resp.text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("ONI text file had no data rows")
    season, year, total, anom = lines[-1].split()[:4]

    anomalies: list[float] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            anomalies.append(float(parts[3]))
        except ValueError:
            continue  # the header row, harmlessly skipped
    diffs = [b - a for a, b in zip(anomalies, anomalies[1:])]
    volatility = statistics.pstdev(diffs) if len(diffs) >= _MIN_CHANGES_FOR_VOLATILITY else None

    return DataPoint(
        label="NOAA Oceanic Nino Index (ONI)",
        value=f"anomaly {float(anom):+.2f} degC (3-month mean Nino 3.4 SST {float(total):.2f} degC)",
        as_of=f"{season} {year}",
        source_url="https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
        numeric_value=float(anom),
        volatility=volatility,
        period_days=30.44,
        is_price=False,
    )


def _fetch_eia_gas_price() -> DataPoint:
    """EIA's weekly US Regular All Formulations Retail Gasoline Price --
    the standard "national average gas price" series these questions ask
    about. Free; EIA_API_KEY is optional (falls back to EIA's own public
    DEMO_KEY, which works but is rate-limited -- set a real free key from
    https://www.eia.gov/opendata/register.php for production use).

    Volatility: stdev of week-over-week log returns, computed from the last
    ~60 weeks (one request either way, just a larger `length` than the
    single latest row this used to fetch) -- a multiplicative/log-normal
    series (price, strictly positive), one sample per 7 days."""
    api_key = os.getenv("EIA_API_KEY") or "DEMO_KEY"
    url = (
        "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
        f"?api_key={api_key}&frequency=weekly&data[0]=value"
        "&facets[product][]=EPMR&facets[duoarea][]=NUS"
        "&sort[0][column]=period&sort[0][direction]=desc&length=60"
    )
    resp = requests.get(url, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    rows = resp.json().get("response", {}).get("data", [])
    if not rows:
        raise ValueError("EIA returned no rows")
    row = rows[0]  # most recent (desc sort)

    values_oldest_first = [float(r["value"]) for r in reversed(rows) if float(r["value"]) > 0]
    log_returns = [math.log(b / a) for a, b in zip(values_oldest_first, values_oldest_first[1:])]
    volatility = statistics.pstdev(log_returns) if len(log_returns) >= _MIN_CHANGES_FOR_VOLATILITY else None

    return DataPoint(
        label="EIA U.S. Regular All Formulations Retail Gasoline Price",
        value=f"${float(row['value']):.3f}/gal",
        as_of=row["period"],
        source_url="https://www.eia.gov/petroleum/gasdiesel/",
        numeric_value=float(row["value"]),
        volatility=volatility,
        period_days=7.0,
        is_price=True,
    )


# To add a new series: write a `_fetch_x() -> DataPoint` function above (GET
# only, raise on any failure -- do not swallow errors here, fetch_direct_data
# below already does) and append one DataSeries entry.
_SERIES: list[DataSeries] = [
    DataSeries(
        name="oni_enso",
        keywords=(
            "el niño", "el nino", "la niña", "la nina", "enso",
            "oceanic niño", "oceanic nino", "oni index", "nino 3.4", "nino3.4",
        ),
        fetch=_fetch_oni,
    ),
    DataSeries(
        name="eia_gas_price",
        keywords=(
            "gas price", "gasoline price", "regular unleaded", "retail gasoline",
            "aaa gas", "price of gas", "pump price",
        ),
        fetch=_fetch_eia_gas_price,
    ),
]


def find_matching_series(question_text: str, resolution_criteria: str | None = None) -> DataSeries | None:
    haystack = f"{question_text or ''} {resolution_criteria or ''}".lower()
    for series in _SERIES:
        if any(kw in haystack for kw in series.keywords):
            return series
    return None


def fetch_direct_data(question_text: str, resolution_criteria: str | None = None) -> DataPoint | None:
    """Returns the matching official DataPoint, or None if nothing matched
    or the fetch itself failed."""
    series = find_matching_series(question_text, resolution_criteria)
    if series is None:
        return None
    try:
        return series.fetch()
    except Exception as e:
        logger.warning(f"Direct data fetch failed for series '{series.name}': {e}")
        return None
