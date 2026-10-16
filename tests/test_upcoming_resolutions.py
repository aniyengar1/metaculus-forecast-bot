"""Tests for scripts/upcoming_resolutions.py: group-question resolve-date
extraction (the bug this guards against: a group post's own top-level
scheduled_resolve_time belongs to only one arbitrary subquestion, not all
of them) and the cumulative-by-horizon report, with fetch_question_json
monkeypatched (no real network call)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import performance_report  # noqa: E402
import upcoming_resolutions as ur  # noqa: E402

from metaculus_bot import db  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def _insert_forecast(conn: sqlite3.Connection, *, question_id: int, post_id: int, run_timestamp: str) -> None:
    db.log_forecast(
        conn,
        question_id=question_id,
        post_id=post_id,
        question_title="title",
        question_type="BinaryQuestion",
        question_url="https://example.com",
        research_summary="",
        raw_model_outputs=[],
        aggregate_value="0.5",
        post_calibration_value="0.5",
        submitted_value="0.5",
        dry_run=False,
        models_used=["m1"],
        n_runs=1,
        cost_usd=0.0,
        status="ok",
        tournament="seasonal",
    )
    conn.execute(
        "UPDATE forecasts SET run_timestamp = ? WHERE question_id = ? AND post_id = ?",
        (run_timestamp, question_id, post_id),
    )
    conn.commit()


def test_extract_scheduled_resolve_plain_post():
    post_json = {"question": {"scheduled_resolve_time": "2026-11-01T00:00:00Z"}}
    assert ur.extract_scheduled_resolve(post_json, 123) == "2026-11-01T00:00:00Z"


def test_extract_scheduled_resolve_group_post_picks_matching_subquestion():
    """The real bug this guards against: a group post's top-level
    scheduled_resolve_time is just one subquestion's value (confirmed
    2026-10-16 against post 43325 -- its top-level field matched only the
    LAST subquestion, two years off for an earlier one)."""
    post_json = {
        "scheduled_resolve_time": "2029-01-01T13:00:00Z",  # belongs to subquestion 43330 only
        "group_of_questions": {
            "questions": [
                {"id": 43330, "scheduled_resolve_time": "2029-01-01T13:00:00Z"},
                {"id": 43329, "scheduled_resolve_time": "2027-01-01T13:00:00Z"},
            ]
        },
    }
    assert ur.extract_scheduled_resolve(post_json, 43329) == "2027-01-01T13:00:00Z"
    assert ur.extract_scheduled_resolve(post_json, 43330) == "2029-01-01T13:00:00Z"


def test_extract_scheduled_resolve_group_post_unknown_subquestion_returns_none():
    post_json = {"group_of_questions": {"questions": [{"id": 1, "scheduled_resolve_time": "2026-11-01T00:00:00Z"}]}}
    assert ur.extract_scheduled_resolve(post_json, 999) is None


def test_generate_report_no_token():
    conn = _conn()
    report = ur.generate_upcoming_resolutions_report(conn, None)
    assert "No METACULUS_TOKEN" in report


def test_generate_report_no_submitted_forecasts():
    conn = _conn()
    report = ur.generate_upcoming_resolutions_report(conn, "fake-token")
    assert "No submitted forecasts" in report


def test_generate_report_cumulative_counts(monkeypatch):
    conn = _conn()
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    _insert_forecast(conn, question_id=1, post_id=100, run_timestamp=now.isoformat())
    _insert_forecast(conn, question_id=2, post_id=101, run_timestamp=now.isoformat())
    _insert_forecast(conn, question_id=3, post_id=102, run_timestamp=now.isoformat())

    resolve_dates = {
        100: "2026-10-20T00:00:00Z",  # +4d
        101: "2026-11-01T00:00:00Z",  # +16d
        102: "2027-02-01T00:00:00Z",  # +108d, beyond all horizons
    }
    monkeypatch.setattr(
        performance_report,
        "fetch_question_json",
        lambda post_id, token: {"question": {"scheduled_resolve_time": resolve_dates[post_id]}},
    )

    report = ur.generate_upcoming_resolutions_report(conn, "fake-token", now=now)
    assert "3 submitted forecasts tracked" in report
    assert "Resolved by +7d" in report and "1/3" in report
    assert "Resolved by +30d" in report and "2/3" in report
    assert "Resolved by +90d" in report and "2/3" in report


def test_generate_report_skips_questions_with_unfetchable_data(monkeypatch):
    conn = _conn()
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    _insert_forecast(conn, question_id=1, post_id=100, run_timestamp=now.isoformat())
    monkeypatch.setattr(performance_report, "fetch_question_json", lambda post_id, token: None)

    report = ur.generate_upcoming_resolutions_report(conn, "fake-token", now=now)
    assert "No submitted forecasts with a known scheduled resolve date" in report
