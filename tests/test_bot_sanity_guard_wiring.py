"""Tests that FutureEvalBot._check_distribution_or_raise actually wires the
sanity guard (tests/test_sanity_guard.py) into the standard skip pattern
(log_alert + a 'skipped_invalid_distribution' forecast row + raise), the
same way _check_budget_or_raise already does for the budget guard. Uses a
lightweight stand-in for `self` (just the 4 attributes the method touches)
rather than constructing a full ForecastBot."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import DateQuestion, NumericDistribution, Percentile

from metaculus_bot import db
from metaculus_bot.bot import FutureEvalBot, InvalidDistributionSkipped

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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def _fake_self(conn: sqlite3.Connection) -> SimpleNamespace:
    return SimpleNamespace(db_conn=conn, dry_run=True, models=["m1"], n_runs=1)


def test_bad_distribution_logs_alert_forecast_row_and_raises():
    conn = _conn()
    fake_self = _fake_self(conn)
    bad_percentiles = [
        Percentile(percentile=0.10, value=datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()),
        Percentile(percentile=0.50, value=datetime(2024, 11, 26, tzinfo=timezone.utc).timestamp()),
        Percentile(percentile=0.90, value=datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
    ]
    bad_dist = NumericDistribution.from_question(bad_percentiles, UKRAINE_DATE_QUESTION)

    with pytest.raises(InvalidDistributionSkipped):
        FutureEvalBot._check_distribution_or_raise(fake_self, UKRAINE_DATE_QUESTION, bad_dist)

    alerts = conn.execute("SELECT message FROM alerts").fetchall()
    assert len(alerts) == 1
    assert "not after now" in alerts[0][0]

    rows = conn.execute("SELECT status, error_message FROM forecasts").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "skipped_invalid_distribution"
    assert "not after now" in rows[0][1]


def test_good_distribution_does_not_raise_or_log():
    conn = _conn()
    fake_self = _fake_self(conn)
    good_percentiles = [
        Percentile(percentile=0.10, value=datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()),
        Percentile(percentile=0.50, value=datetime(2027, 2, 1, tzinfo=timezone.utc).timestamp()),
        Percentile(percentile=0.90, value=datetime(2027, 5, 20, tzinfo=timezone.utc).timestamp()),
    ]
    good_dist = NumericDistribution.from_question(good_percentiles, UKRAINE_DATE_QUESTION)

    FutureEvalBot._check_distribution_or_raise(fake_self, UKRAINE_DATE_QUESTION, good_dist)  # must not raise

    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0
