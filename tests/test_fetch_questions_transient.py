"""
Regression test for a real incident (2026-10-20): a GitHub Actions seasonal
run failed and emailed a failure alert because a dependency download blipped
mid-install. That specific case is now handled by the poetry-install retry
script, but it exposed a broader gap -- an unhandled exception while *fetching*
questions (a prolonged Metaculus blip, a provider rate-limit surfacing through
the SDK, etc.) would likewise crash the whole run and spam a failure email even
when the very next scheduled/dispatched tick a few minutes later would have
succeeded on its own.

Fix: main.py wraps the question-fetching section in a try/except that consults
is_transient_provider_or_metaculus_error -- a transient blip logs an alert and
exits 0 (the run is a no-op, not a failure), while a genuine error (auth
failure, code bug, "model not found") still logs an alert *and* re-raises so the
run fails loudly. These tests exercise that wiring end-to-end through _run(),
not just the classifier in isolation (which test_transient_errors.py covers).
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import litellm
import pytest

import main as main_module
from metaculus_bot import db


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        mode="tournament",
        target="minibench",
        post_ids=None,
        publish=False,
        limit=None,
        type_diverse=False,
    )


def _patch_run_prerequisites(monkeypatch, tmp_path):
    """Stub out everything _run() touches before the fetch try/except so the
    only thing under test is how a fetch exception is handled."""
    db_path = str(tmp_path / "forecasts.db")
    monkeypatch.setattr(main_module, "load_config", lambda: SimpleNamespace(db_path=db_path))
    monkeypatch.setattr(main_module, "check_environment", lambda strict=True: None)
    monkeypatch.setattr(main_module, "print_startup_banner", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_check_rate_limit_guard", lambda cfg: True)
    monkeypatch.setattr(main_module, "MetaculusClient", lambda *a, **k: object())
    return db_path


def _alerts(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(db.SCHEMA)
        return [row[0] for row in conn.execute("SELECT message FROM alerts ORDER BY id")]
    finally:
        conn.close()


def test_transient_fetch_error_logs_alert_and_exits_zero(monkeypatch, tmp_path):
    """A provider/Metaculus blip during fetch must NOT fail the run: log an
    alert and return 0 so a one-off outage doesn't trigger a failure email."""
    db_path = _patch_run_prerequisites(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise litellm.exceptions.ServiceUnavailableError("upstream 503", "model", "provider")

    monkeypatch.setattr(main_module, "_fetch_questions", boom)

    exit_code = asyncio.run(main_module._run(_args()))

    assert exit_code == 0
    alerts = _alerts(db_path)
    assert len(alerts) == 1
    assert "Transient error fetching questions" in alerts[0]
    assert "ServiceUnavailableError" in alerts[0]


def test_genuine_fetch_error_logs_alert_and_reraises(monkeypatch, tmp_path):
    """A genuine error (here an auth failure) must still fail the run loudly --
    log an alert AND propagate, never silently swallow a real bug."""
    db_path = _patch_run_prerequisites(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise litellm.exceptions.AuthenticationError("bad key", "model", "provider")

    monkeypatch.setattr(main_module, "_fetch_questions", boom)

    with pytest.raises(litellm.exceptions.AuthenticationError):
        asyncio.run(main_module._run(_args()))

    alerts = _alerts(db_path)
    assert len(alerts) == 1
    assert "Non-transient error fetching questions" in alerts[0]
    assert "AuthenticationError" in alerts[0]


def test_generic_code_bug_during_fetch_reraises(monkeypatch, tmp_path):
    """A plain Python bug (e.g. an AttributeError from a refactor) is the most
    important thing NOT to swallow -- it must fail the run."""
    db_path = _patch_run_prerequisites(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise AttributeError("'NoneType' object has no attribute 'close_time'")

    monkeypatch.setattr(main_module, "_fetch_questions", boom)

    with pytest.raises(AttributeError):
        asyncio.run(main_module._run(_args()))

    alerts = _alerts(db_path)
    assert len(alerts) == 1
    assert "Non-transient error fetching questions" in alerts[0]
