"""Tests for scripts/dispatch_health.py's pure helpers (expected-count math,
PAT-expiry warning) plus generate_dispatch_health_report() with
count_dispatch_runs monkeypatched (no real network call)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch_health as dh  # noqa: E402


def test_expected_dispatch_count():
    assert dh.expected_dispatch_count(7) == 1008  # 7 days * 24h * 6/hour
    assert dh.expected_dispatch_count(1, interval_minutes=15) == 96


def test_pat_expiry_warning_none_when_not_set():
    assert dh.pat_expiry_warning(None, datetime(2026, 10, 13, tzinfo=timezone.utc)) is None
    assert dh.pat_expiry_warning("", datetime(2026, 10, 13, tzinfo=timezone.utc)) is None


def test_pat_expiry_warning_none_when_far_out():
    warning = dh.pat_expiry_warning("2027-06-01", datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert warning is None


def test_pat_expiry_warning_within_window():
    warning = dh.pat_expiry_warning("2026-10-20", datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert warning is not None
    assert "7 day" in warning


def test_pat_expiry_warning_already_expired():
    warning = dh.pat_expiry_warning("2026-10-01", datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert warning is not None
    assert "EXPIRED" in warning


def test_pat_expiry_warning_unparseable():
    warning = dh.pat_expiry_warning("not-a-date", datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert warning is not None
    assert "Could not parse" in warning


def test_generate_dispatch_health_report_no_token():
    report = dh.generate_dispatch_health_report("owner/repo", None, None)
    assert "No GITHUB_TOKEN" in report


def test_generate_dispatch_health_report_counts_and_rate(monkeypatch):
    counts = {"run_bot_on_minibench.yaml": 504, "run_bot_on_seasonal.yaml": 1008}
    monkeypatch.setattr(dh, "count_dispatch_runs", lambda repo, wf, token, since: counts[wf])

    report = dh.generate_dispatch_health_report("owner/repo", "fake-token", None, days=7)
    assert "run_bot_on_minibench.yaml: 504/1008 dispatches landed in last 7d (50%)" in report
    assert "run_bot_on_seasonal.yaml: 1008/1008 dispatches landed in last 7d (100%)" in report


def test_generate_dispatch_health_report_api_failure_reported_per_workflow(monkeypatch):
    monkeypatch.setattr(dh, "count_dispatch_runs", lambda repo, wf, token, since: None)
    report = dh.generate_dispatch_health_report("owner/repo", "fake-token", None, days=7)
    assert "could not check (API error)" in report


def test_generate_dispatch_health_report_includes_expiry_warning(monkeypatch):
    monkeypatch.setattr(dh, "count_dispatch_runs", lambda repo, wf, token, since: 1008)
    report = dh.generate_dispatch_health_report(
        "owner/repo", "fake-token", "2026-10-15", days=7, today=datetime(2026, 10, 13, tzinfo=timezone.utc)
    )
    assert "WARNING" in report
    assert "expires in" in report
