"""Tests that FutureEvalBot._binary_prompt wires in the status-quo note
(metaculus_bot/status_quo.py) for "will X happen by/before deadline" shaped
questions, and leaves it out otherwise."""
from __future__ import annotations

from forecasting_tools import BinaryQuestion

from metaculus_bot.bot import FutureEvalBot

DEADLINE_QUESTION = BinaryQuestion(
    id_of_question=1,
    id_of_post=1,
    question_text="Will AST publish the practical-driving-test calendar before 12 November 2026?",
    page_url="https://www.metaculus.com/questions/1",
    background_info="",
    resolution_criteria="",
    fine_print="",
)

RACE_QUESTION = BinaryQuestion(
    id_of_question=2,
    id_of_post=2,
    question_text="Will the 2026 World Series require a Game 6?",
    page_url="https://www.metaculus.com/questions/2",
    background_info="",
    resolution_criteria="",
    fine_print="",
)


def test_deadline_question_includes_status_quo_note():
    prompt = FutureEvalBot._binary_prompt(DEADLINE_QUESTION, "research text")
    assert "base rate" in prompt.lower()


def test_non_deadline_question_omits_status_quo_note():
    prompt = FutureEvalBot._binary_prompt(RACE_QUESTION, "research text")
    assert "base rate" not in prompt.lower()
