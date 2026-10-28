"""Tests for metaculus_bot.db's resolution-tracking helpers (record_resolution,
count_tracked_binary_resolutions) and the resolutions-table migration. See
docs/STAGE_B_PLAN.md "Calibration tracking" -- pure logging, no correction
applied anywhere based on this yet."""
from __future__ import annotations

import sqlite3

from metaculus_bot import db


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def test_resolutions_table_has_raw_ensemble_prob_column_after_migration():
    conn = _conn()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(resolutions)")}
    assert "raw_ensemble_prob" in columns


def test_migration_adds_column_to_pre_existing_resolutions_table():
    """Simulates a persisted data-branch DB created before this column
    existed -- CREATE TABLE IF NOT EXISTS is a no-op against it, so only the
    explicit ALTER TABLE migration can add the column."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE forecasts (id INTEGER PRIMARY KEY, status TEXT NOT NULL, dry_run INTEGER NOT NULL);
        CREATE TABLE resolutions (
            question_id INTEGER PRIMARY KEY,
            resolved_at TEXT,
            resolution_value TEXT,
            brier_score REAL,
            log_score REAL
        );
        """
    )
    columns_before = {row[1] for row in conn.execute("PRAGMA table_info(resolutions)")}
    assert "raw_ensemble_prob" not in columns_before

    db._migrate(conn)

    columns_after = {row[1] for row in conn.execute("PRAGMA table_info(resolutions)")}
    assert "raw_ensemble_prob" in columns_after


def test_record_resolution_inserts_new_row():
    conn = _conn()
    db.record_resolution(
        conn, question_id=1, resolved_at="2026-10-20T22:31:00Z", resolution_value="yes", raw_ensemble_prob=0.803
    )
    row = conn.execute(
        "SELECT resolved_at, resolution_value, raw_ensemble_prob FROM resolutions WHERE question_id = 1"
    ).fetchone()
    assert row == ("2026-10-20T22:31:00Z", "yes", 0.803)


def test_record_resolution_is_idempotent_upsert():
    conn = _conn()
    db.record_resolution(
        conn, question_id=1, resolved_at="2026-10-20T22:31:00Z", resolution_value="yes", raw_ensemble_prob=0.803
    )
    db.record_resolution(
        conn, question_id=1, resolved_at="2026-10-20T22:31:00Z", resolution_value="yes", raw_ensemble_prob=0.803
    )
    count = conn.execute("SELECT COUNT(*) FROM resolutions WHERE question_id = 1").fetchone()[0]
    assert count == 1


def test_count_tracked_binary_resolutions():
    conn = _conn()
    assert db.count_tracked_binary_resolutions(conn) == 0
    db.record_resolution(conn, question_id=1, resolved_at="t1", resolution_value="yes", raw_ensemble_prob=0.6)
    db.record_resolution(conn, question_id=2, resolved_at="t2", resolution_value="no", raw_ensemble_prob=0.4)
    assert db.count_tracked_binary_resolutions(conn) == 2
