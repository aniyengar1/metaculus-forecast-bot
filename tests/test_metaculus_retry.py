"""
Confirms the SDK's MetaculusClient actually retries with backoff on
429/5xx for both question-fetching and forecast/comment submission, the
same way scripts/performance_report.py's own direct fetch_question_json
does (see docs/STAGE_B_PLAN.md "Backup poller" era work for that fix).

Traced the source (forecasting_tools 0.2.x, pinned range in
pyproject.toml) to confirm: get_question_by_post_id, _get_questions_from_api
(used by get_all_open_questions_from_tournament/get_questions_matching_filter,
i.e. our question fetching), post_question_comment, and
_post_question_prediction (used by all three post_*_question_prediction
submission methods) are each wrapped in
forecasting_tools.util.misc.retry_with_exponential_backoff(), which retries
up to 3 times on any requests.exceptions.RequestException -- a 429 or 5xx
response raises requests.exceptions.HTTPError via
raise_for_status_with_additional_info, which is caught (HTTPError is a
RequestException subclass). bot.py/main.py call these SDK methods directly
and make no other Metaculus HTTP calls of their own, so this already
covers the main bot end to end -- these tests prove it behaviorally
(mocking requests.get/post to fail once with 429 then succeed) rather than
just asserting a decorator is present, so a future forecasting_tools
version silently dropping this would be caught here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from forecasting_tools import MetaculusClient

_FIXTURE_QUESTION_JSON = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "metaculus_question_sample.json").read_text()
)


class _FakeResponse:
    def __init__(self, status_code: int, json_body: object = None, url: str = "https://www.metaculus.com/api/x"):
        self.status_code = status_code
        self.reason = "error" if status_code >= 400 else "OK"
        self.text = ""
        self.url = url
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._json_body

    @property
    def content(self) -> bytes:
        return json.dumps(self._json_body).encode()


def _client() -> MetaculusClient:
    client = MetaculusClient(token="fake-token")
    client.sleep_time_between_requests_min = 0
    client.sleep_jitter_seconds = 0
    return client


def test_get_question_by_post_id_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses = [_FakeResponse(429), _FakeResponse(200, _FIXTURE_QUESTION_JSON)]
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(requests, "get", fake_get)
    client = _client()

    question = client.get_question_by_post_id(_FIXTURE_QUESTION_JSON["id"])

    assert len(calls) == 2  # first call 429'd, second succeeded
    assert question.id_of_post == _FIXTURE_QUESTION_JSON["id"]


def test_get_question_by_post_id_retries_on_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses = [_FakeResponse(503), _FakeResponse(200, _FIXTURE_QUESTION_JSON)]
    monkeypatch.setattr(requests, "get", lambda *a, **k: responses.pop(0))
    client = _client()

    question = client.get_question_by_post_id(_FIXTURE_QUESTION_JSON["id"])
    assert question.id_of_post == _FIXTURE_QUESTION_JSON["id"]


def test_get_question_by_post_id_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(429))
    client = _client()

    try:
        client.get_question_by_post_id(_FIXTURE_QUESTION_JSON["id"])
        assert False, "expected HTTPError after exhausting retries"
    except requests.exceptions.HTTPError:
        pass


def test_post_question_comment_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses = [_FakeResponse(429), _FakeResponse(204)]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    client = _client()

    client.post_question_comment(12345, "short comment")  # must not raise

    assert len(calls) == 2


def test_post_binary_prediction_retries_on_429_then_succeeds(monkeypatch):
    """post_binary_question_prediction delegates to _post_question_prediction,
    the shared decorated submission path also used by post_numeric_ and
    post_multiple_choice_question_prediction."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses = [_FakeResponse(429), _FakeResponse(201)]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    client = _client()

    client.post_binary_question_prediction(12345, 0.6)  # must not raise

    assert len(calls) == 2
