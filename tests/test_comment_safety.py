"""
Metaculus treats long public bot comments as spam (penalties up to account
deactivation). Confirmed via the live API 2026-10-13 that our comments are
already private (8/8, 0 public) but far too long (6-11k chars). These tests
cover the fix: metaculus_bot.bot._short_value_repr (short per-model
summaries instead of full objects) and _force_private_and_cap_length (an
instance-level wrapper forcing is_private=True and a length cap on every
post_question_comment call, regardless of caller).
"""
from __future__ import annotations

from datetime import datetime, timezone

from forecasting_tools import NumericDistribution, Percentile, PredictedOption, PredictedOptionList

from metaculus_bot.bot import _MAX_COMMENT_CHARS, _force_private_and_cap_length, _short_value_repr


def test_short_value_repr_binary_float():
    assert _short_value_repr(0.7) == "70.0%"
    assert _short_value_repr(0.01) == "1.0%"


def test_short_value_repr_multiple_choice():
    value = PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name="Low", probability=0.3),
            PredictedOption(option_name="Medium", probability=0.5),
            PredictedOption(option_name="High", probability=0.2),
        ]
    )
    repr_str = _short_value_repr(value)
    assert len(repr_str) <= 80
    assert "Low=30%" in repr_str
    assert "Medium=50%" in repr_str


def test_short_value_repr_numeric_distribution_is_short_not_full_dump():
    percentiles = [Percentile(percentile=p / 100, value=v) for p, v in [(10, 1), (50, 5), (90, 9)]]
    dist = NumericDistribution(
        declared_percentiles=percentiles,
        open_upper_bound=False,
        open_lower_bound=False,
        upper_bound=10.0,
        lower_bound=0.0,
        zero_point=None,
    )
    repr_str = _short_value_repr(dist)
    assert len(repr_str) < 40  # vs. a full repr, which is thousands of chars (201 declared_percentiles)
    assert repr_str.startswith("median")


def test_short_value_repr_date_distribution_formats_as_date():
    now = datetime.now(timezone.utc).timestamp()
    percentiles = [
        Percentile(percentile=0.1, value=now),
        Percentile(percentile=0.5, value=now + 86400 * 10),
        Percentile(percentile=0.9, value=now + 86400 * 20),
    ]
    dist = NumericDistribution(
        declared_percentiles=percentiles,
        open_upper_bound=False,
        open_lower_bound=False,
        upper_bound=now + 86400 * 30,
        lower_bound=now,
        zero_point=None,
        is_date=True,
    )
    repr_str = _short_value_repr(dist)
    assert len(repr_str) < 30
    assert "-" in repr_str  # looks like a YYYY-MM-DD, not a raw timestamp float


def test_short_value_repr_falls_back_for_unknown_type_and_truncates():
    class Weird:
        def __repr__(self):
            return "x" * 500

    repr_str = _short_value_repr(Weird())
    assert len(repr_str) <= 80


class _FakeClient:
    """Stands in for MetaculusClient: just needs a post_question_comment
    attribute to be monkey-patched, and records what it was actually called
    with so we can assert on it."""

    def __init__(self):
        self.calls = []

    def post_question_comment(self, post_id, comment_text, is_private=True, included_forecast=True):
        self.calls.append((post_id, comment_text, is_private, included_forecast))


def test_force_private_forces_true_even_when_caller_passes_false():
    client = _FakeClient()
    _force_private_and_cap_length(client)
    client.post_question_comment(123, "short comment", is_private=False)
    assert len(client.calls) == 1
    _, text, is_private, _ = client.calls[0]
    assert is_private is True  # forced, regardless of what the caller asked for
    assert text == "short comment"


def test_force_private_caps_length():
    client = _FakeClient()
    _force_private_and_cap_length(client)
    long_text = "a" * 20000
    client.post_question_comment(123, long_text)
    _, text, is_private, _ = client.calls[0]
    assert is_private is True
    assert len(text) <= _MAX_COMMENT_CHARS
    assert text.endswith("[truncated]")


def test_force_private_leaves_short_comment_unmodified_besides_privacy():
    client = _FakeClient()
    _force_private_and_cap_length(client)
    short_text = "Final prediction: 42.0%\n\nPer-model (3):\n- a: 40%\n- b: 45%\n- c: 41%"
    client.post_question_comment(123, short_text)
    _, text, _, _ = client.calls[0]
    assert text == short_text
