"""Tests for the DB primitives behind seasonal re-forecasting (main.py's
_select_reforecast_candidates uses these to decide what's stale and how
much of today's cap is left)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from metaculus_bot import db


def _insert_ok_forecast(conn: sqlite3.Connection, question_id: int, when: datetime) -> None:
    conn.execute(
        """
        INSERT INTO forecasts (run_timestamp, question_id, dry_run, status)
        VALUES (?, ?, 0, 'ok')
        """,
        (when.isoformat(), question_id),
    )
    conn.commit()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def test_get_last_forecast_time_none_when_never_forecast():
    conn = _conn()
    assert db.get_last_forecast_time(conn, 999) is None


def test_get_last_forecast_time_returns_most_recent():
    conn = _conn()
    now = datetime.now(timezone.utc)
    _insert_ok_forecast(conn, 1, now - timedelta(days=10))
    _insert_ok_forecast(conn, 1, now - timedelta(days=2))
    last = db.get_last_forecast_time(conn, 1)
    assert last is not None
    assert abs((last - (now - timedelta(days=2))).total_seconds()) < 1


def test_count_reforecasts_today_zero_when_all_first_time():
    conn = _conn()
    now = datetime.now(timezone.utc)
    _insert_ok_forecast(conn, 1, now)
    _insert_ok_forecast(conn, 2, now)
    assert db.count_reforecasts_today(conn) == 0


def test_count_reforecasts_today_counts_genuine_reforecasts_only():
    conn = _conn()
    now = datetime.now(timezone.utc)
    # Question 1: forecast 10 days ago, re-forecast today -- counts.
    _insert_ok_forecast(conn, 1, now - timedelta(days=10))
    _insert_ok_forecast(conn, 1, now)
    # Question 2: forecast for the first time today -- does not count.
    _insert_ok_forecast(conn, 2, now)
    assert db.count_reforecasts_today(conn) == 1
