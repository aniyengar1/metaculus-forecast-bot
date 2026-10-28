"""
Classifies an exception as "transient" (a provider/Metaculus blip -- safe
to log an alert and let the run exit cleanly rather than crash and send a
failure email) vs "genuine" (an auth error, a real code bug, or every
model being down -- must still fail the run loudly). See
docs/STAGE_B_PLAN.md "Transient vs genuine failure classification" for the
incident this guards against.

Deliberately conservative: only a known, explicit set of transient
exception types/HTTP statuses are treated as safe to swallow. Everything
else (AttributeError, TypeError, a litellm.AuthenticationError, a 404 for
a retired/misconfigured model, etc.) propagates and fails the run --
silently swallowing a real bug is worse than one noisy failure email.
"""
from __future__ import annotations

import litellm
import requests

_TRANSIENT_LITELLM_EXCEPTIONS = (
    litellm.exceptions.RateLimitError,  # 429
    litellm.exceptions.ServiceUnavailableError,  # 503
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.InternalServerError,  # 500
    litellm.exceptions.BadGatewayError,  # 502
)

_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def is_transient_provider_or_metaculus_error(exc: BaseException) -> bool:
    """True for a rate limit, timeout, connection error, or 5xx -- the
    kind of thing that's very likely to succeed on its own a few minutes
    later without anyone doing anything. False (the safe default) for
    everything else, including auth errors and "model not found"."""
    if isinstance(exc, _TRANSIENT_LITELLM_EXCEPTIONS):
        return True
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in _TRANSIENT_HTTP_STATUSES
    return False
