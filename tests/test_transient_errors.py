"""Tests for metaculus_bot.transient_errors.is_transient_provider_or_metaculus_error.
Deliberately conservative: a known, explicit allowlist of transient
types/statuses, everything else must still fail the run."""
from __future__ import annotations

import litellm
import requests

from metaculus_bot.transient_errors import is_transient_provider_or_metaculus_error


def test_litellm_rate_limit_error_is_transient():
    assert is_transient_provider_or_metaculus_error(litellm.exceptions.RateLimitError("x", "model", "provider"))


def test_litellm_service_unavailable_is_transient():
    assert is_transient_provider_or_metaculus_error(
        litellm.exceptions.ServiceUnavailableError("x", "model", "provider")
    )


def test_litellm_timeout_is_transient():
    assert is_transient_provider_or_metaculus_error(litellm.exceptions.Timeout("x", "model", "provider"))


def test_litellm_authentication_error_is_not_transient():
    assert not is_transient_provider_or_metaculus_error(
        litellm.exceptions.AuthenticationError("x", "model", "provider")
    )


def test_litellm_not_found_error_is_not_transient():
    assert not is_transient_provider_or_metaculus_error(litellm.exceptions.NotFoundError("x", "model", "provider"))


def test_requests_connection_error_is_transient():
    assert is_transient_provider_or_metaculus_error(requests.exceptions.ConnectionError("x"))


def test_requests_timeout_is_transient():
    assert is_transient_provider_or_metaculus_error(requests.exceptions.Timeout("x"))


def test_requests_http_error_503_is_transient():
    response = requests.Response()
    response.status_code = 503
    exc = requests.exceptions.HTTPError("x", response=response)
    assert is_transient_provider_or_metaculus_error(exc)


def test_requests_http_error_429_is_transient():
    response = requests.Response()
    response.status_code = 429
    exc = requests.exceptions.HTTPError("x", response=response)
    assert is_transient_provider_or_metaculus_error(exc)


def test_requests_http_error_401_is_not_transient():
    response = requests.Response()
    response.status_code = 401
    exc = requests.exceptions.HTTPError("x", response=response)
    assert not is_transient_provider_or_metaculus_error(exc)


def test_requests_http_error_404_is_not_transient():
    response = requests.Response()
    response.status_code = 404
    exc = requests.exceptions.HTTPError("x", response=response)
    assert not is_transient_provider_or_metaculus_error(exc)


def test_requests_http_error_without_response_is_not_transient():
    exc = requests.exceptions.HTTPError("x")
    assert not is_transient_provider_or_metaculus_error(exc)


def test_generic_python_exceptions_are_not_transient():
    assert not is_transient_provider_or_metaculus_error(ValueError("x"))
    assert not is_transient_provider_or_metaculus_error(TypeError("x"))
    assert not is_transient_provider_or_metaculus_error(AttributeError("x"))
    assert not is_transient_provider_or_metaculus_error(KeyError("x"))
