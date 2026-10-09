"""
SQLite logging for every forecast the bot produces, plus a resolutions table
to be filled in later (once questions resolve) and an asknews_usage table to
track the monthly call cap across runs.

Schema is intentionally denormalized (JSON text columns for nested data) since
this is an analysis log, not a transactional store.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    question_id INTEGER,
    post_id INTEGER,
    question_title TEXT,
    question_type TEXT,
    question_url TEXT,
    research_summary TEXT,
    raw_model_outputs TEXT,
    aggregate_value TEXT,
    post_calibration_value TEXT,
    submitted_value TEXT,
    dry_run INTEGER NOT NULL,
    models_used TEXT,
    n_runs INTEGER,
    cost_usd REAL,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
    question_id INTEGER PRIMARY KEY,
    resolved_at TEXT,
    resolution_value TEXT,
    brier_score REAL,
    log_score REAL
);

CREATE TABLE IF NOT EXISTS asknews_usage (
    month TEXT PRIMARY KEY,
    calls_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_questions (
    question_id INTEGER PRIMARY KEY,
    post_id INTEGER,
    question_title TEXT,
    question_url TEXT,
    close_time TEXT,
    first_seen_at TEXT NOT NULL,
    forecasted INTEGER NOT NULL DEFAULT 0,
    closed_unforecast_alerted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def log_forecast(
    conn: sqlite3.Connection,
    *,
    question_id: int | None,
    post_id: int | None,
    question_title: str,
    question_type: str,
    question_url: str,
    research_summary: str,
    raw_model_outputs: list[dict[str, Any]],
    aggregate_value: Any,
    post_calibration_value: Any,
    submitted_value: Any,
    dry_run: bool,
    models_used: list[str],
    n_runs: int,
    cost_usd: float | None,
    status: str,
    error_message: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO forecasts (
            run_timestamp, question_id, post_id, question_title, question_type,
            question_url, research_summary, raw_model_outputs, aggregate_value,
            post_calibration_value, submitted_value, dry_run, models_used,
            n_runs, cost_usd, status, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            question_id,
            post_id,
            question_title,
            question_type,
            question_url,
            research_summary,
            json.dumps(raw_model_outputs, default=str),
            json.dumps(aggregate_value, default=str),
            json.dumps(post_calibration_value, default=str),
            json.dumps(submitted_value, default=str),
            1 if dry_run else 0,
            json.dumps(models_used),
            n_runs,
            cost_usd,
            status,
            error_message,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_cost_spent_today(conn: sqlite3.Connection) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM forecasts "
        "WHERE substr(run_timestamp, 1, 10) = ? AND status = 'ok'",
        (today,),
    ).fetchone()
    return float(row[0] or 0.0)


def get_questions_forecast_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) FROM forecasts "
        "WHERE substr(run_timestamp, 1, 10) = ? AND status = 'ok'",
        (today,),
    ).fetchone()
    return int(row[0] or 0)


def get_asknews_usage(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT calls_used FROM asknews_usage WHERE month = ?", (month,)
    ).fetchone()
    return int(row[0]) if row else 0


def log_alert(conn: sqlite3.Connection, message: str) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (timestamp, message) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), message),
    )
    conn.commit()
    return cur.lastrowid


def increment_asknews_usage(conn: sqlite3.Connection, month: str, n: int = 1) -> int:
    conn.execute(
        """
        INSERT INTO asknews_usage (month, calls_used) VALUES (?, ?)
        ON CONFLICT(month) DO UPDATE SET calls_used = calls_used + excluded.calls_used
        """,
        (month, n),
    )
    conn.commit()
    return get_asknews_usage(conn, month)


def upsert_seen_question(
    conn: sqlite3.Connection,
    *,
    question_id: int,
    post_id: int | None,
    question_title: str,
    question_url: str,
    close_time_iso: str | None,
) -> None:
    """Records that we've observed this open question, if not already
    tracked. Does not overwrite forecasted/alerted flags on an existing row."""
    conn.execute(
        """
        INSERT INTO seen_questions
            (question_id, post_id, question_title, question_url, close_time, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(question_id) DO NOTHING
        """,
        (
            question_id,
            post_id,
            question_title,
            question_url,
            close_time_iso,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def mark_question_forecasted(conn: sqlite3.Connection, question_id: int) -> None:
    conn.execute(
        "UPDATE seen_questions SET forecasted = 1 WHERE question_id = ?", (question_id,)
    )
    conn.commit()


def find_newly_closed_unforecast(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """Questions we saw while open, never forecast, whose close_time has now
    passed, and haven't already been alerted on. Returns
    (question_id, title, url, close_time)."""
    now = datetime.now(timezone.utc).isoformat()
    return conn.execute(
        """
        SELECT question_id, question_title, question_url, close_time
        FROM seen_questions
        WHERE forecasted = 0
          AND closed_unforecast_alerted = 0
          AND close_time IS NOT NULL
          AND close_time < ?
        """,
        (now,),
    ).fetchall()


def mark_closed_unforecast_alerted(conn: sqlite3.Connection, question_id: int) -> None:
    conn.execute(
        "UPDATE seen_questions SET closed_unforecast_alerted = 1 WHERE question_id = ?",
        (question_id,),
    )
    conn.commit()
