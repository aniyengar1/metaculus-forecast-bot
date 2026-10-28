"""Tests for scripts/performance_report.py's pure helpers (score_data
extraction, resolution parsing, CP-at-forecast-time lookup, Brier/
calibration math) plus an end-to-end generate_report() test against an
in-memory DB with fetch_question_json monkeypatched (no real network call).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import performance_report as pr  # noqa: E402

from metaculus_bot import db  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


class _FakeResponse:
    def __init__(self, status_code: int, json_body: object = None):
        self.status_code = status_code
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


def test_fetch_question_json_success_no_retry_needed(monkeypatch):
    monkeypatch.setattr(pr.time, "sleep", lambda *_: None)
    calls = []
    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: calls.append(1) or _FakeResponse(200, {"ok": True}))
    assert pr.fetch_question_json(1, "token") == {"ok": True}
    assert len(calls) == 1


def test_fetch_question_json_retries_once_after_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(pr.time, "sleep", lambda s: sleeps.append(s))
    responses = [_FakeResponse(429), _FakeResponse(200, {"ok": True})]
    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: responses.pop(0))
    assert pr.fetch_question_json(1, "token") == {"ok": True}
    assert pr._RETRY_AFTER_429_SECONDS in sleeps  # backed off before the retry


def test_fetch_question_json_two_429s_gives_up_and_returns_none(monkeypatch):
    monkeypatch.setattr(pr.time, "sleep", lambda *_: None)
    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: _FakeResponse(429))
    assert pr.fetch_question_json(1, "token") is None


def test_is_resolved():
    assert pr.is_resolved({"question": {"resolution": "yes"}})
    assert not pr.is_resolved({"question": {"resolution": None}})
    assert not pr.is_resolved({"question": {}})


def test_parse_binary_outcome():
    assert pr.parse_binary_outcome({"question": {"resolution": "yes"}}) == 1.0
    assert pr.parse_binary_outcome({"question": {"resolution": "no"}}) == 0.0
    assert pr.parse_binary_outcome({"question": {"resolution": "annulled"}}) is None
    assert pr.parse_binary_outcome({"question": {"resolution": None}}) is None


def test_extract_score_data_prefers_non_spot():
    qjson = {
        "question": {
            "my_forecasts": {
                "score_data": {
                    "peer_score": 10.0,
                    "baseline_score": 5.0,
                    "spot_peer_score": 99.0,
                    "spot_baseline_score": 99.0,
                    "coverage": 0.9,
                }
            }
        }
    }
    peer, baseline, coverage = pr.extract_score_data(qjson)
    assert peer == 10.0
    assert baseline == 5.0
    assert coverage == 0.9


def test_extract_score_data_falls_back_to_spot_when_non_spot_absent():
    qjson = {
        "question": {
            "my_forecasts": {
                "score_data": {
                    "spot_peer_score": 57.5,
                    "spot_baseline_score": 35.1,
                    "coverage": 0.8,
                }
            }
        }
    }
    peer, baseline, coverage = pr.extract_score_data(qjson)
    assert peer == 57.5
    assert baseline == 35.1


def test_extract_score_data_empty_when_unscored():
    peer, baseline, coverage = pr.extract_score_data({"question": {"my_forecasts": {"score_data": {}}}})
    assert peer is None
    assert baseline is None
    assert coverage is None


def test_community_prediction_at_picks_latest_entry_before_timestamp():
    qjson = {
        "question": {
            "aggregations": {
                "recency_weighted": {
                    "history": [
                        {"start_time": 100.0, "end_time": 200.0, "centers": [0.3]},
                        {"start_time": 200.0, "end_time": 300.0, "centers": [0.5]},
                        {"start_time": 300.0, "end_time": None, "centers": [0.7]},
                    ]
                }
            }
        }
    }
    assert pr.community_prediction_at(qjson, 250.0) == 0.5
    assert pr.community_prediction_at(qjson, 50.0) is None
    assert pr.community_prediction_at(qjson, 1000.0) == 0.7


def test_community_prediction_at_no_history_returns_none():
    assert pr.community_prediction_at({"question": {"aggregations": {}}}, 100.0) is None


def test_brier_score():
    assert pr.brier_score(1.0, 1.0) == 0.0
    assert pr.brier_score(0.0, 1.0) == 1.0
    assert abs(pr.brier_score(0.7, 1.0) - 0.09) < 1e-9


def test_calibration_table_bins_and_empirical_rate():
    pairs = [(0.05, 0.0), (0.08, 0.0), (0.95, 1.0), (0.92, 1.0), (0.91, 0.0)]
    table = pr.calibration_table(pairs)
    low_bin = next(b for b in table if b["bin_low"] == 0.0)
    assert low_bin["count"] == 2
    assert low_bin["empirical_rate"] == 0.0
    high_bin = next(b for b in table if b["bin_low"] == 0.9)
    assert high_bin["count"] == 3
    assert abs(high_bin["empirical_rate"] - (2 / 3)) < 1e-9


def test_calibration_table_edge_value_one_goes_in_last_bin():
    table = pr.calibration_table([(1.0, 1.0)])
    assert len(table) == 1
    assert table[0]["bin_low"] == 0.9


def _insert_forecast(
    conn: sqlite3.Connection,
    *,
    question_id: int,
    post_id: int,
    question_type: str,
    tournament: str,
    dry_run: int,
    run_timestamp: str,
    aggregate_value: str | None = None,
    post_calibration_value: str | None = None,
    raw_model_outputs: list | None = None,
) -> None:
    # aggregate_value/post_calibration_value are passed as plain strings
    # here (e.g. "0.6"), matching bot.py's _record_debug which already does
    # str(value) before calling log_forecast -- log_forecast's own
    # json.dumps(..., default=str) then double-encodes on top of that,
    # exactly like production (see performance_report._parse_logged_value).
    db.log_forecast(
        conn,
        question_id=question_id,
        post_id=post_id,
        question_title="title",
        question_type=question_type,
        question_url="https://example.com",
        research_summary="",
        raw_model_outputs=raw_model_outputs or [],
        aggregate_value=aggregate_value,
        post_calibration_value=post_calibration_value,
        submitted_value=post_calibration_value,
        dry_run=bool(dry_run),
        models_used=["m1"],
        n_runs=1,
        cost_usd=0.0,
        status="ok",
        tournament=tournament,
    )
    conn.execute(
        "UPDATE forecasts SET run_timestamp = ? WHERE question_id = ? AND post_id = ?",
        (run_timestamp, question_id, post_id),
    )
    conn.commit()


def test_generate_report_no_token():
    conn = _conn()
    assert "No METACULUS_TOKEN" in pr.generate_report(conn, None)


def test_generate_report_end_to_end(monkeypatch):
    conn = _conn()
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    _insert_forecast(
        conn,
        question_id=1,
        post_id=100,
        question_type="BinaryQuestion",
        tournament="minibench",
        dry_run=0,
        run_timestamp=ts,
        aggregate_value="0.6",
        post_calibration_value="0.55",
        raw_model_outputs=[
            {"model": "gemini/gemini-3.8-flash", "parsed_value": "0.7", "error": None},
            {"model": "groq/openai/gpt-oss-120b", "parsed_value": "0.5", "error": None},
        ],
    )

    fake_question_json = {
        "question": {
            "resolution": "yes",
            "my_forecasts": {"score_data": {"peer_score": 12.3, "baseline_score": 4.5, "coverage": 1.0}},
            "aggregations": {
                "recency_weighted": {
                    "history": [{"start_time": datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp(), "end_time": None, "centers": [0.4]}]
                }
            },
        }
    }
    monkeypatch.setattr(pr, "fetch_question_json", lambda post_id, token: fake_question_json)

    report = pr.generate_report(conn, "fake-token")
    assert "Resolved + scored: 1" in report
    assert "avg peer=12.30" in report
    assert "gemini/gemini-3.8-flash" in report
    assert "groq/openai/gpt-oss-120b" in report
    assert "Raw ensemble Brier" in report
    assert "Post-calibration Brier" in report
    assert "community prediction at forecast time" in report
    assert "Calibration tracking: 1 resolved binary question(s) logged" in report

    tracked = conn.execute(
        "SELECT resolution_value, raw_ensemble_prob FROM resolutions WHERE question_id = 1"
    ).fetchone()
    assert tuple(tracked) == ("yes", 0.6)  # raw (pre-calibration) aggregate_value, not post_calibration_value's 0.55


def test_generate_report_skips_dry_run_for_official_score_but_not_local_brier(monkeypatch):
    conn = _conn()
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    _insert_forecast(
        conn,
        question_id=2,
        post_id=200,
        question_type="BinaryQuestion",
        tournament="test",
        dry_run=1,  # never actually submitted
        run_timestamp=ts,
        aggregate_value="0.3",
        post_calibration_value="0.35",
        raw_model_outputs=[{"model": "gemini/gemini-3.8-flash", "parsed_value": "0.3", "error": None}],
    )
    fake_question_json = {
        "question": {
            "resolution": "no",
            "my_forecasts": {"score_data": {}},  # never submitted -> never scored
            "aggregations": {},
        }
    }
    monkeypatch.setattr(pr, "fetch_question_json", lambda post_id, token: fake_question_json)

    report = pr.generate_report(conn, "fake-token")
    assert "No resolved-and-scored questions yet" in report
    assert "Binary questions resolved (local analysis): 1" in report
