"""
Pre-flight check against OpenRouter's /api/v1/key endpoint, which reports
the account's current free-model daily request usage without spending one of
those requests (it's a metadata endpoint, not a model call). Used by main.py
to warn/refuse local or manually-dispatched runs when there isn't enough
quota left for the next scheduled production run -- local testing and the
scheduled MiniBench run share the same account-wide 50-or-1000/day cap (see
docs/STAGE_B_PLAN.md).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_KEY_INFO_URL = "https://openrouter.ai/api/v1/key"
_TIMEOUT_SECONDS = 10


@dataclass
class QuotaStatus:
    used: int
    limit: int
    remaining: int


def get_free_model_daily_quota() -> QuotaStatus | None:
    """Returns None if the check itself fails (no API key, network error,
    unexpected response shape) -- callers should treat that as 'unknown,
    proceed with a warning' rather than blocking on an inability to check."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        response = requests.get(
            _KEY_INFO_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        fmdr = response.json()["data"]["free_model_daily_requests"]
        return QuotaStatus(used=fmdr["used"], limit=fmdr["limit"], remaining=fmdr["remaining"])
    except Exception as e:
        logger.warning(f"Could not check OpenRouter free-model quota: {e}")
        return None
