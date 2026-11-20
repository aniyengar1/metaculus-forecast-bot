"""Tests for metaculus_bot/data_index.py -- keyword matching and
graceful-failure behavior only. Deliberately does NOT hit the live NOAA/EIA
endpoints in CI (slow, flaky, and burns a real request every test run);
`fetch()` is monkeypatched instead. The real endpoints were hand-verified
against the live services while building this (NOAA ONI ascii file, EIA v2
API with DEMO_KEY) -- see docs/STAGE_B_PLAN.md."""
from __future__ import annotations

import pytest

from metaculus_bot import data_index
from metaculus_bot.data_index import DataPoint, DataSeries, _fetch_eia_gas_price, _fetch_oni, fetch_direct_data, find_matching_series


class TestFindMatchingSeries:
    def test_matches_oni_series_on_el_nino_question(self):
        series = find_matching_series("Will the August-October 2026 El Niño index be 2.10°C or higher?")
        assert series is not None
        assert series.name == "oni_enso"

    def test_matches_gas_price_series(self):
        series = find_matching_series("What will the US national average gas price be?")
        assert series is not None
        assert series.name == "eia_gas_price"

    def test_matches_against_resolution_criteria_too(self):
        series = find_matching_series(
            "Some generic question title", "Resolves based on the AAA gas price report."
        )
        assert series is not None
        assert series.name == "eia_gas_price"

    def test_no_match_for_unrelated_question(self):
        series = find_matching_series("Will the 2026 World Series require a Game 6?")
        assert series is None


class TestFetchDirectData:
    def test_returns_none_when_no_series_matches(self):
        assert fetch_direct_data("Will the 2026 World Series require a Game 6?") is None

    def test_returns_datapoint_on_successful_fetch(self, monkeypatch):
        fake_point = DataPoint(label="fake", value="1.0", as_of="2026-11-09", source_url="http://example.com")
        fake_series = DataSeries(name="fake_series", keywords=("fakekeyword",), fetch=lambda: fake_point)
        monkeypatch.setattr(data_index, "_SERIES", [fake_series])

        result = fetch_direct_data("question containing fakekeyword")
        assert result is fake_point

    def test_returns_none_and_does_not_raise_when_fetch_fails(self, monkeypatch):
        def _boom() -> DataPoint:
            raise RuntimeError("network down")

        fake_series = DataSeries(name="broken_series", keywords=("fakekeyword",), fetch=_boom)
        monkeypatch.setattr(data_index, "_SERIES", [fake_series])

        assert fetch_direct_data("question containing fakekeyword") is None


class TestDataPointResearchBlock:
    def test_as_research_block_includes_date_and_source(self):
        point = DataPoint(
            label="NOAA Oceanic Nino Index (ONI)",
            value="anomaly +2.46 degC",
            as_of="ASO 2026",
            source_url="https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
        )
        block = point.as_research_block()
        assert "ASO 2026" in block
        assert "+2.46" in block
        assert "cpc.ncep.noaa.gov" in block


@pytest.mark.parametrize(
    "question_text",
    [
        "Will the August-October 2026 El Niño index be 2.10°C or higher?",
        "What will the US national average gas price be on November 3, 2026?",
    ],
)
def test_real_series_registry_still_matches_known_question_shapes(question_text):
    """Not a network test -- just confirms the real (unpatched) _SERIES list
    still recognizes the two question shapes this was built for."""
    assert find_matching_series(question_text) is not None


class _FakeResponse:
    def __init__(self, text: str = "", json_data: dict | None = None):
        self._text = text
        self._json = json_data

    def raise_for_status(self) -> None:
        pass

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> dict:
        return self._json


# 13 monthly ONI anomalies alternating +0.10/-0.10 -- an odd count gives an
# even number of consecutive diffs (12), split exactly 6/6 between -0.20
# and +0.20, so their mean is exactly 0 and pstdev is exactly 0.2 -- a
# clean, known number rather than something only checkable by eyeballing a
# real multi-decade file.
_FAKE_ONI_TEXT = "SEAS YEAR TOTAL ANOM\n" + "\n".join(
    f"XXX {2000 + i // 12} 27.00 {0.10 if i % 2 == 0 else -0.10:+.2f}" for i in range(13)
)


class TestFetchOniVolatility:
    def test_populates_numeric_fields_from_full_history(self, monkeypatch):
        monkeypatch.setattr(data_index.requests, "get", lambda *a, **k: _FakeResponse(text=_FAKE_ONI_TEXT))
        point = _fetch_oni()
        assert point.numeric_value == pytest.approx(0.10)  # last of 13 (index 12, even) is +0.10
        assert point.is_price is False
        assert point.period_days == pytest.approx(30.44)
        # 12 diffs split exactly 6/6 between -0.2 and +0.2 -- mean 0, pstdev 0.2 exactly.
        assert point.volatility == pytest.approx(0.2, abs=1e-9)

    def test_too_few_rows_leaves_volatility_none(self, monkeypatch):
        short_text = "SEAS YEAR TOTAL ANOM\nXXX 2026 27.00 +0.50\nYYY 2026 27.10 +0.60"
        monkeypatch.setattr(data_index.requests, "get", lambda *a, **k: _FakeResponse(text=short_text))
        point = _fetch_oni()
        assert point.numeric_value == pytest.approx(0.60)
        assert point.volatility is None


_FAKE_EIA_ROWS_DESC = [
    {"period": f"2026-W{52 - i:02d}", "value": str(4.0 * (1.01**i) if i % 2 == 0 else 4.0 * (0.99**i))}
    for i in range(10)
]


class TestFetchEiaGasPriceVolatility:
    def test_populates_numeric_fields_from_history(self, monkeypatch):
        fake_json = {"response": {"data": _FAKE_EIA_ROWS_DESC}}
        monkeypatch.setattr(data_index.requests, "get", lambda *a, **k: _FakeResponse(json_data=fake_json))
        point = _fetch_eia_gas_price()
        assert point.numeric_value == pytest.approx(float(_FAKE_EIA_ROWS_DESC[0]["value"]))
        assert point.is_price is True
        assert point.period_days == pytest.approx(7.0)
        assert point.volatility is not None and point.volatility > 0

    def test_too_few_rows_leaves_volatility_none(self, monkeypatch):
        fake_json = {"response": {"data": _FAKE_EIA_ROWS_DESC[:3]}}
        monkeypatch.setattr(data_index.requests, "get", lambda *a, **k: _FakeResponse(json_data=fake_json))
        point = _fetch_eia_gas_price()
        assert point.volatility is None
