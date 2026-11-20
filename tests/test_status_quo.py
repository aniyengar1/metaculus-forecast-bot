"""Tests for metaculus_bot/status_quo.py -- the "will X happen by/before
deadline Y" question-shape detector that adds an explicit status-quo-default
instruction to the binary prompt."""
from __future__ import annotations

from metaculus_bot.status_quo import STATUS_QUO_PROMPT_ADDITION, is_status_quo_question


class TestIsStatusQuoQuestion:
    def test_will_by_date_is_status_quo(self):
        assert is_status_quo_question("Will the FAA issue an amended type certificate by 2027-01-01?") is True

    def test_will_before_date_is_status_quo(self):
        assert (
            is_status_quo_question(
                "Will AST publish the practical-driving-test calendar before 12 November 2026?"
            )
            is True
        )

    def test_will_before_without_explicit_date_token_is_still_status_quo(self):
        # The deadline is often in resolution_criteria, not the title itself.
        assert is_status_quo_question("Will Spain's Congress convalidate the decree before the deadline?") is True

    def test_two_sided_race_is_not_status_quo(self):
        assert is_status_quo_question("Will the 2026 World Series require a Game 6?") is False

    def test_empty_text_is_not_status_quo(self):
        assert is_status_quo_question("") is False

    def test_will_without_by_or_before_is_not_status_quo(self):
        assert is_status_quo_question("Will Ukraine strike a Russian oil refinery this week?") is False


def test_prompt_addition_mentions_base_rate_and_status_quo():
    assert "base rate" in STATUS_QUO_PROMPT_ADDITION.lower()
    assert "status-quo" in STATUS_QUO_PROMPT_ADDITION.lower() or "status quo" in STATUS_QUO_PROMPT_ADDITION.lower()
