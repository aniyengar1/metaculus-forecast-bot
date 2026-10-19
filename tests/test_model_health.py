"""
Regression tests for a real incident (2026-10-19): health-checking every
model in the pool on every run -- including all 4 OpenRouter fallback
candidates, even when Gemini/Groq (the primary tier) were healthy and
never actually needed the fallback -- burned OpenRouter's entire 50/day
account-wide free-tier cap within ~2 hours once the GCP VM backup poller
started running every ~10 min. Measured that day: 40/40 of OpenRouter's
usage was health checks, 0 real forecast calls.

Fix: the OpenRouter fallback tier is only health-checked if the primary
(non-OpenRouter) models don't already satisfy target_count, and every
health check result is cached in SQLite for 1 hour regardless of tier.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import metaculus_bot.model_health as model_health
from metaculus_bot import db
from metaculus_bot.model_health import HealthResult, select_healthy_models


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


def test_fallback_tier_not_checked_when_primary_satisfies_target(monkeypatch):
    """The actual bug: 3 healthy primaries + a target of 3 must mean the
    OpenRouter fallback models are never even pinged."""
    checked = []

    async def fake_check_one(model):
        checked.append(model)
        return HealthResult(model=model, healthy=True)

    monkeypatch.setattr(model_health, "_check_one", fake_check_one)

    pool = [
        "gemini/gemini-3.8-flash",
        "groq/openai/gpt-oss-120b",
        "groq/qwen/qwen3.8-27b",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/apodex/apodex-1.1-mini:free",
    ]
    conn = _conn()

    selected, results = asyncio.run(select_healthy_models(conn, pool, target_count=3))

    assert selected == pool[:3]
    assert "openrouter/nvidia/nemotron-3-super-120b-a12b:free" not in checked
    assert "openrouter/apodex/apodex-1.1-mini:free" not in checked
    assert checked == pool[:3]


def test_fallback_tier_checked_when_primary_insufficient(monkeypatch):
    checked = []

    async def fake_check_one(model):
        checked.append(model)
        healthy = "openrouter" in model  # only the fallback tier is healthy this time
        return HealthResult(model=model, healthy=healthy)

    monkeypatch.setattr(model_health, "_check_one", fake_check_one)

    pool = [
        "gemini/gemini-3.8-flash",
        "groq/openai/gpt-oss-120b",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    ]
    conn = _conn()

    selected, results = asyncio.run(select_healthy_models(conn, pool, target_count=2))

    assert "openrouter/nvidia/nemotron-3-super-120b-a12b:free" in checked
    assert selected == ["openrouter/nvidia/nemotron-3-super-120b-a12b:free"]


def test_health_result_cached_within_ttl_skips_recheck(monkeypatch):
    call_count = {"n": 0}

    async def fake_check_one(model):
        call_count["n"] += 1
        return HealthResult(model=model, healthy=True)

    monkeypatch.setattr(model_health, "_check_one", fake_check_one)

    conn = _conn()
    pool = ["gemini/gemini-3.8-flash"]

    asyncio.run(select_healthy_models(conn, pool, target_count=1))
    assert call_count["n"] == 1

    asyncio.run(select_healthy_models(conn, pool, target_count=1))
    assert call_count["n"] == 1  # second call reused the cache, no new check


def test_health_result_rechecked_after_ttl_expires(monkeypatch):
    call_count = {"n": 0}

    async def fake_check_one(model):
        call_count["n"] += 1
        return HealthResult(model=model, healthy=True)

    monkeypatch.setattr(model_health, "_check_one", fake_check_one)

    conn = _conn()
    model = "gemini/gemini-3.8-flash"
    db.set_cached_model_health(conn, model, True, None)
    # Backdate the cache entry past the 1h TTL.
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE model_health_cache SET checked_at = ? WHERE model = ?", (stale_time, model))
    conn.commit()

    asyncio.run(select_healthy_models(conn, [model], target_count=1))
    assert call_count["n"] == 1  # stale cache ignored, re-checked fresh


def test_get_cached_model_health_roundtrip():
    conn = _conn()
    assert db.get_cached_model_health(conn, "m1", max_age_seconds=3600) is None

    db.set_cached_model_health(conn, "m1", True, None)
    assert db.get_cached_model_health(conn, "m1", max_age_seconds=3600) == (True, None)

    db.set_cached_model_health(conn, "m1", False, "RateLimitError: x")
    assert db.get_cached_model_health(conn, "m1", max_age_seconds=3600) == (False, "RateLimitError: x")


def test_get_cached_model_health_respects_max_age():
    conn = _conn()
    db.set_cached_model_health(conn, "m1", True, None)
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    conn.execute("UPDATE model_health_cache SET checked_at = ? WHERE model = ?", (stale_time, "m1"))
    conn.commit()
    assert db.get_cached_model_health(conn, "m1", max_age_seconds=3600) is None
