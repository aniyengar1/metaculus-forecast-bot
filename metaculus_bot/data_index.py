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
import os
from dataclasses import dataclass
from typing import Callable

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15


@dataclass
class DataPoint:
    label: str
    value: str
    as_of: str
    source_url: str

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
    recent period is the last non-empty line."""
    resp = requests.get(
        "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt", timeout=_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    lines = [line for line in resp.text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("ONI text file had no data rows")
    season, year, total, anom = lines[-1].split()[:4]
    return DataPoint(
        label="NOAA Oceanic Nino Index (ONI)",
        value=f"anomaly {float(anom):+.2f} degC (3-month mean Nino 3.4 SST {float(total):.2f} degC)",
        as_of=f"{season} {year}",
        source_url="https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
    )


def _fetch_eia_gas_price() -> DataPoint:
    """EIA's weekly US Regular All Formulations Retail Gasoline Price --
    the standard "national average gas price" series these questions ask
    about. Free; EIA_API_KEY is optional (falls back to EIA's own public
    DEMO_KEY, which works but is rate-limited -- set a real free key from
    https://www.eia.gov/opendata/register.php for production use)."""
    api_key = os.getenv("EIA_API_KEY") or "DEMO_KEY"
    url = (
        "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
        f"?api_key={api_key}&frequency=weekly&data[0]=value"
        "&facets[product][]=EPMR&facets[duoarea][]=NUS"
        "&sort[0][column]=period&sort[0][direction]=desc&length=1"
    )
    resp = requests.get(url, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    rows = resp.json().get("response", {}).get("data", [])
    if not rows:
        raise ValueError("EIA returned no rows")
    row = rows[0]
    return DataPoint(
        label="EIA U.S. Regular All Formulations Retail Gasoline Price",
        value=f"${float(row['value']):.3f}/gal",
        as_of=row["period"],
        source_url="https://www.eia.gov/petroleum/gasdiesel/",
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
