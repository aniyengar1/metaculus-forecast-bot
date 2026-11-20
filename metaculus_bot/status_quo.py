"""
Detects "will X happen by/before <deadline>" framed binary questions, so the
binary prompt can add an explicit status-quo-default instruction for them.

Why a separate check from the existing "good forecasters put extra weight on
the status quo" line already in the binary prompt: that line is generic
advice given on every binary question. This module targets the specific
shape where status quo has a clear, nameable default (the deadline passes
without the event happening) -- "will X happen by/before Y" -- rather than
questions with no obvious default (e.g. a close two-candidate race), where
leaning on "status quo" doesn't actually mean anything.
"""
from __future__ import annotations

import re

# "Will ... by|before ..." anywhere in the question, allowing the usual
# amount of text in between (background clauses, named entities, etc.) --
# deliberately loose rather than requiring a literal date token right after
# by/before, since the deadline is often spelled out in resolution_criteria
# instead of the question title itself.
_STATUS_QUO_PATTERN = re.compile(r"\bwill\b.{0,200}\b(by|before)\b", re.IGNORECASE | re.DOTALL)


def is_status_quo_question(question_text: str) -> bool:
    """True if this reads as a "will X happen by/before deadline Y" binary
    question, where the status quo (nothing happens) is a nameable default
    outcome distinct from "the event occurs"."""
    return bool(_STATUS_QUO_PATTERN.search(question_text or ""))


# Appended to the binary prompt only when is_status_quo_question() is True.
# Two things the user asked for explicitly: (1) default toward the status
# quo unless research shows concrete scheduled/in-progress evidence, (2)
# state the base rate explicitly rather than leaving it implicit in the
# final number.
STATUS_QUO_PROMPT_ADDITION = (
    "This question asks whether something will happen by a deadline. Most "
    "scheduled or anticipated events slip or fail to materialize within a "
    "specific window, so default toward the status-quo outcome (usually "
    "No) unless your research shows concrete, scheduled, or already-in-"
    "progress evidence that it is happening -- a confirmed date, an "
    "announced event, an action already underway. State your estimated "
    "base rate for this general type of near-term deadline event "
    "explicitly (e.g. \"base rate for this kind of event ~15-25%\") before "
    "adjusting it for the specific evidence in your research."
)
