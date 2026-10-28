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
    estimated_cost_usd REAL,
    tournament TEXT,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
    question_id INTEGER PRIMARY KEY,
    resolved_at TEXT,
    resolution_value TEXT,
    brier_score REAL,
    log_score REAL,
    raw_ensemble_prob REAL
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

CREATE TABLE IF NOT EXISTS provider_usage (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    date TEXT NOT NULL,
    requests_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, model, date)
);

CREATE TABLE IF NOT EXISTS provider_learned_limits (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    rpd_limit INTEGER NOT NULL,
    learned_at TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);

CREATE TABLE IF NOT EXISTS model_health_cache (
    model TEXT PRIMARY KEY,
    healthy INTEGER NOT NULL,
    error TEXT,
    checked_at TEXT NOT NULL
);
"""


# Columns added after a table's first release. CREATE TABLE IF NOT EXISTS is
# a no-op against an already-existing table (e.g. the persisted data-branch
# DB from earlier runs), so new columns need an explicit migration rather
# than just appearing in SCHEMA above.
_FORECASTS_TABLE_MIGRATIONS = [
    ("estimated_cost_usd", "REAL"),
    ("tournament", "TEXT"),
]

_RESOLUTIONS_TABLE_MIGRATIONS = [
    ("raw_ensemble_prob", "REAL"),
]


def _add_missing_columns(conn: sqlite3.Connection, table: str, migrations: list[tuple[str, str]]) -> None:
    existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, sql_type in migrations:
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _migrate(conn: sqlite3.Connection) -> None:
    _add_missing_columns(conn, "forecasts", _FORECASTS_TABLE_MIGRATIONS)
    _add_missing_columns(conn, "resolutions", _RESOLUTIONS_TABLE_MIGRATIONS)
    conn.commit()


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
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
    estimated_cost_usd: float | None = None,
    tournament: str | None = None,
    error_message: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO forecasts (
            run_timestamp, question_id, post_id, question_title, question_type,
            question_url, research_summary, raw_model_outputs, aggregate_value,
            post_calibration_value, submitted_value, dry_run, models_used,
            n_runs, cost_usd, estimated_cost_usd, tournament, status, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            estimated_cost_usd,
            tournament,
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


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def increment_provider_usage(
    conn: sqlite3.Connection, provider: str, model: str, n: int = 1
) -> int:
    today = _today()
    conn.execute(
        """
        INSERT INTO provider_usage (provider, model, date, requests_used) VALUES (?, ?, ?, ?)
        ON CONFLICT(provider, model, date) DO UPDATE SET requests_used = requests_used + excluded.requests_used
        """,
        (provider, model, today, n),
    )
    conn.commit()
    return get_provider_usage_today(conn, provider, model)


def get_provider_usage_today(conn: sqlite3.Connection, provider: str, model: str) -> int:
    row = conn.execute(
        "SELECT requests_used FROM provider_usage WHERE provider = ? AND model = ? AND date = ?",
        (provider, model, _today()),
    ).fetchone()
    return int(row[0]) if row else 0


def set_learned_rpd_limit(conn: sqlite3.Connection, provider: str, model: str, rpd: int) -> None:
    conn.execute(
        """
        INSERT INTO provider_learned_limits (provider, model, rpd_limit, learned_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(provider, model) DO UPDATE SET rpd_limit = excluded.rpd_limit, learned_at = excluded.learned_at
        """,
        (provider, model, rpd, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_learned_rpd_limit(conn: sqlite3.Connection, provider: str, model: str) -> int | None:
    row = conn.execute(
        "SELECT rpd_limit FROM provider_learned_limits WHERE provider = ? AND model = ?",
        (provider, model),
    ).fetchone()
    return int(row[0]) if row else None


def set_cached_model_health(conn: sqlite3.Connection, model: str, healthy: bool, error: str | None) -> None:
    conn.execute(
        """
        INSERT INTO model_health_cache (model, healthy, error, checked_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(model) DO UPDATE SET healthy = excluded.healthy, error = excluded.error, checked_at = excluded.checked_at
        """,
        (model, 1 if healthy else 0, error, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_cached_model_health(
    conn: sqlite3.Connection, model: str, *, healthy_ttl_seconds: int, unhealthy_ttl_seconds: int
) -> tuple[bool, str | None] | None:
    """Returns (healthy, error) if a health check for this model was cached
    recently enough, else None (caller should check fresh). Which TTL
    applies depends on the *cached* verdict: `unhealthy_ttl_seconds` is
    deliberately shorter than `healthy_ttl_seconds` -- a 2026-10-27 audit
    found Gemini outages are short-lived (single-timestamp 503/500/Timeout
    blips, not sustained), so caching "dead" for a full hour needlessly
    drops it from every run in that window even after it's recovered. See
    metaculus_bot/model_health.py -- this is also what stops the
    OpenRouter fallback tier (and everything else) from being re-pinged on
    every single run when healthy."""
    row = conn.execute(
        "SELECT healthy, error, checked_at FROM model_health_cache WHERE model = ?", (model,)
    ).fetchone()
    if not row:
        return None
    healthy = bool(row[0])
    ttl = healthy_ttl_seconds if healthy else unhealthy_ttl_seconds
    checked_at = datetime.fromisoformat(row[2])
    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
    if age_seconds > ttl:
        return None
    return healthy, row[1]


def get_last_forecast_time(conn: sqlite3.Connection, question_id: int) -> datetime | None:
    """Most recent successful (status='ok') forecast timestamp for this
    question, from our own log -- used to decide whether a still-open
    seasonal question is stale enough to re-forecast."""
    row = conn.execute(
        "SELECT MAX(run_timestamp) FROM forecasts WHERE question_id = ? AND status = 'ok'",
        (question_id,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0])


def latest_forecasts_per_question(conn: sqlite3.Connection, *, dry_run: bool | None) -> list[sqlite3.Row]:
    """One row per question_id -- whichever 'ok' forecast is most recent (a
    re-forecast supersedes an earlier one for analysis purposes). Shared by
    scripts/performance_report.py and scripts/upcoming_resolutions.py.

    dry_run=False restricts to rows that were actually submitted to
    Metaculus (the only ones that can have an official score, or a
    resolve-date worth tracking as "our" prediction); dry_run=None
    considers both, for local-only metrics (Brier/calibration) that don't
    need an official submission, just our own predicted probability and the
    eventual outcome."""
    conn.row_factory = sqlite3.Row
    outer_clause = "" if dry_run is None else "AND f.dry_run = 0"
    inner_clause = "" if dry_run is None else "AND f2.dry_run = 0"
    rows = conn.execute(
        f"""
        SELECT f.* FROM forecasts f
        WHERE f.status = 'ok' AND f.question_id IS NOT NULL {outer_clause}
          AND f.run_timestamp = (
              SELECT MAX(f2.run_timestamp) FROM forecasts f2
              WHERE f2.question_id = f.question_id AND f2.status = 'ok' {inner_clause}
          )
        """
    ).fetchall()
    return rows


def record_resolution(
    conn: sqlite3.Connection,
    *,
    question_id: int,
    resolved_at: str | None,
    resolution_value: str | None,
    raw_ensemble_prob: float,
) -> None:
    """Logs a resolved binary question's raw (pre-calibration) ensemble
    probability alongside the eventual outcome, purely for later analysis --
    no correction is applied anywhere based on this yet. Idempotent: a
    question's resolution never changes once set, so re-running this on an
    already-recorded question just overwrites it with the same values. See
    docs/STAGE_B_PLAN.md "Calibration tracking"."""
    conn.execute(
        """
        INSERT INTO resolutions (question_id, resolved_at, resolution_value, raw_ensemble_prob)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(question_id) DO UPDATE SET
            resolved_at = excluded.resolved_at,
            resolution_value = excluded.resolution_value,
            raw_ensemble_prob = excluded.raw_ensemble_prob
        """,
        (question_id, resolved_at, resolution_value, raw_ensemble_prob),
    )
    conn.commit()


def count_tracked_binary_resolutions(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM resolutions WHERE raw_ensemble_prob IS NOT NULL"
    ).fetchone()
    return int(row[0] or 0)


def count_reforecasts_today(conn: sqlite3.Connection) -> int:
    """How many of today's successful forecasts are a *re*-forecast (i.e.
    this question already had an earlier successful forecast, today or any
    prior day) -- for the max_reforecasts_per_day cap."""
    today = _today()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM forecasts f1
        WHERE f1.status = 'ok' AND substr(f1.run_timestamp, 1, 10) = ?
          AND EXISTS (
              SELECT 1 FROM forecasts f2
              WHERE f2.question_id = f1.question_id
                AND f2.status = 'ok'
                AND f2.run_timestamp < f1.run_timestamp
          )
        """,
        (today,),
    ).fetchone()
    return int(row[0] or 0)
