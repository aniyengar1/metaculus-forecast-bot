"""Tests for scripts/backfill_parsed_values.py: migrating the pre-2026-10-18
parsed_value format (a Python str(value) repr) to the new clean JSON
format, extracted from the repr itself (never re-parsing raw_text, which
can be lossy -- see metaculus_bot.bot._structured_parsed_value)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_parsed_values as bf  # noqa: E402

from metaculus_bot import db  # noqa: E402


def test_backfill_entry_binary_exact_float():
    value, reason = bf.backfill_entry({"parsed_value": "0.62", "error": None}, "BinaryQuestion")
    assert reason is None
    assert value == 0.62


def test_backfill_entry_binary_unparseable():
    value, reason = bf.backfill_entry({"parsed_value": "not-a-number", "error": None}, "BinaryQuestion")
    assert value is None
    assert reason is not None


def test_backfill_entry_multiple_choice_extracts_all_options():
    old_repr = (
        "PredictedOptionList(predicted_options=["
        "PredictedOption(option_name='Low', probability=0.3), "
        "PredictedOption(option_name='High', probability=0.7)])"
    )
    value, reason = bf.backfill_entry({"parsed_value": old_repr, "error": None}, "MultipleChoiceQuestion")
    assert reason is None
    assert value == {"Low": 0.3, "High": 0.7}


def test_backfill_entry_numeric_extracts_representative_percentiles():
    old_repr = (
        "declared_percentiles=[Percentile(percentile=0.0, value=3.5), "
        "Percentile(percentile=0.5, value=4.2), "
        "Percentile(percentile=1.0, value=6.0)] open_upper_bound=True"
    )
    value, reason = bf.backfill_entry({"parsed_value": old_repr, "error": None}, "NumericQuestion")
    assert reason is None
    assert len(value) == 3
    percentiles = [p["percentile"] for p in value]
    assert 0.0 in percentiles and 0.5 in percentiles and 1.0 in percentiles


def test_backfill_entry_already_clean_is_noop():
    """New-format entries (float/dict/list, not a string) pass through
    unchanged -- idempotent, safe to re-run."""
    value, reason = bf.backfill_entry({"parsed_value": 0.6, "error": None}, "BinaryQuestion")
    assert reason is None
    assert value == 0.6


def test_backfill_entry_error_rows_stay_none():
    value, reason = bf.backfill_entry({"parsed_value": None, "error": "RateLimitError"}, "BinaryQuestion")
    assert reason is None
    assert value is None


def test_backfill_entry_unknown_question_type():
    value, reason = bf.backfill_entry({"parsed_value": "0.6", "error": None}, "SomeNewQuestionType")
    assert value is None
    assert reason is not None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def _insert_row(conn: sqlite3.Connection, *, post_id: int, question_type: str, raw_model_outputs: list) -> None:
    db.log_forecast(
        conn,
        question_id=post_id,
        post_id=post_id,
        question_title="t",
        question_type=question_type,
        question_url="https://x",
        research_summary="",
        raw_model_outputs=raw_model_outputs,
        aggregate_value=None,
        post_calibration_value=None,
        submitted_value=None,
        dry_run=False,
        models_used=["m"],
        n_runs=1,
        cost_usd=0.0,
        status="ok",
    )


def test_backfill_all_dry_run_does_not_write(monkeypatch):
    conn = _conn()
    _insert_row(
        conn, post_id=1, question_type="BinaryQuestion",
        raw_model_outputs=[{"model": "m1", "parsed_value": "0.6", "error": None}],
    )
    result = bf.backfill_all(conn, apply=False)
    assert result["migrated_entries"] == 1

    raw = conn.execute("SELECT raw_model_outputs FROM forecasts WHERE post_id=1").fetchone()[0]
    stored = json.loads(raw)[0]["parsed_value"]
    assert stored == "0.6"  # unchanged, since apply=False


def test_backfill_all_apply_writes_clean_value():
    conn = _conn()
    _insert_row(
        conn, post_id=1, question_type="BinaryQuestion",
        raw_model_outputs=[{"model": "m1", "parsed_value": "0.6", "error": None}],
    )
    result = bf.backfill_all(conn, apply=True)
    assert result["migrated_entries"] == 1
    assert result["failures"] == []

    raw = conn.execute("SELECT raw_model_outputs FROM forecasts WHERE post_id=1").fetchone()[0]
    stored = json.loads(raw)[0]["parsed_value"]
    assert stored == 0.6
    assert isinstance(stored, float)


def test_backfill_all_is_idempotent():
    """Running it twice must not change already-migrated rows or double-count."""
    conn = _conn()
    _insert_row(
        conn, post_id=1, question_type="BinaryQuestion",
        raw_model_outputs=[{"model": "m1", "parsed_value": "0.6", "error": None}],
    )
    bf.backfill_all(conn, apply=True)
    second = bf.backfill_all(conn, apply=True)
    assert second["migrated_entries"] == 0
    assert second["already_clean_entries"] == 1


def test_backfill_all_reports_failures_without_crashing():
    conn = _conn()
    _insert_row(
        conn, post_id=1, question_type="BinaryQuestion",
        raw_model_outputs=[{"model": "m1", "parsed_value": "garbage", "error": None}],
    )
    result = bf.backfill_all(conn, apply=True)
    assert len(result["failures"]) == 1
    assert result["failures"][0]["post_id"] == 1
    assert result["failures"][0]["model"] == "m1"
