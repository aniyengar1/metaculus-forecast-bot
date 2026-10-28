"""Tests for metaculus_bot.llm_retry.invoke_with_backoff's retry allowlist.
Regression coverage for a real incident (2026-10-27): an ensemble-composition
audit found Gemini missing from ~70% of eligible forecasts, and 100% of
sampled incidents were ServiceUnavailableError (503), InternalServerError
(500), or Timeout -- none of which were retried before this, since only
RateLimitError (429) was covered. See docs/STAGE_B_PLAN.md "Ensemble
composition audit + Gemini reliability plan"."""
from __future__ import annotations

import asyncio

import litellm
import pytest

from metaculus_bot.llm_retry import invoke_with_backoff


class _FakeLlm:
    def __init__(self, exceptions_then_result):
        self._queue = list(exceptions_then_result)
        self.model = "fake/model"
        self.calls = 0

    async def invoke(self, prompt: str) -> str:
        self.calls += 1
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def _no_sleep(_seconds):
    return None


@pytest.mark.parametrize(
    "exc",
    [
        litellm.exceptions.RateLimitError("x", "model", "provider"),
        litellm.exceptions.ServiceUnavailableError("x", "model", "provider"),
        litellm.exceptions.InternalServerError("x", "model", "provider"),
        litellm.exceptions.Timeout("x", "model", "provider"),
    ],
)
def test_retries_each_allowlisted_exception_then_succeeds(monkeypatch, exc):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    llm = _FakeLlm([exc, "OK"])
    result = asyncio.run(invoke_with_backoff(llm, "prompt"))
    assert result == "OK"
    assert llm.calls == 2


def test_gives_up_after_max_tries_exhausted(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    exc = litellm.exceptions.ServiceUnavailableError("still down", "model", "provider")
    llm = _FakeLlm([exc, exc, exc])
    with pytest.raises(litellm.exceptions.ServiceUnavailableError):
        asyncio.run(invoke_with_backoff(llm, "prompt", max_tries=3))
    assert llm.calls == 3


def test_non_retryable_exception_propagates_immediately():
    llm = _FakeLlm([litellm.exceptions.AuthenticationError("bad key", "model", "provider")])
    with pytest.raises(litellm.exceptions.AuthenticationError):
        asyncio.run(invoke_with_backoff(llm, "prompt"))
    assert llm.calls == 1


def test_backs_off_exponentially_between_attempts(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    exc = litellm.exceptions.Timeout("x", "model", "provider")
    llm = _FakeLlm([exc, exc, "OK"])
    result = asyncio.run(invoke_with_backoff(llm, "prompt", max_tries=3))
    assert result == "OK"
    assert sleeps == [2, 4]
