"""Regression test for a real incident (2026-10-30): the old flat
DEFAULT_MODEL_POOL (gemini-3.5-flash, gemini-3.8-flash, then the two Groq
models) meant a fully healthy run selected the first 3 entries in list
order -- both Gemini models -- and dropped qwen3.8-27b entirely, losing
provider diversity for no reason. Fix: slot-based structure, one model per
slot. See docs/STAGE_B_PLAN.md "Slot-based model selection"."""
from __future__ import annotations

from metaculus_bot.config import DEFAULT_MODEL_SLOTS, DEFAULT_OPENROUTER_FALLBACK_POOL
from metaculus_bot.model_naming import provider_of


def test_three_slots_gemini_groq120b_groqqwen():
    assert len(DEFAULT_MODEL_SLOTS) == 3
    assert DEFAULT_MODEL_SLOTS[0] == ["gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash"]
    assert DEFAULT_MODEL_SLOTS[1] == ["groq/openai/gpt-oss-120b"]
    assert DEFAULT_MODEL_SLOTS[2] == ["groq/qwen/qwen3.8-27b"]


def test_gemini_slot_prefers_38_flash_over_35_flash():
    gemini_slot = DEFAULT_MODEL_SLOTS[0]
    assert gemini_slot.index("gemini/gemini-3.8-flash") < gemini_slot.index("gemini/gemini-3.5-flash")


def test_no_slot_candidate_is_openrouter():
    """OpenRouter models only ever come from the separate fallback pool,
    never as a named slot candidate -- a slot's own candidates are always
    distinct, non-OpenRouter providers."""
    for slot in DEFAULT_MODEL_SLOTS:
        for model in slot:
            assert provider_of(model) != "openrouter"


def test_openrouter_fallback_pool_has_multiple_candidates():
    assert len(DEFAULT_OPENROUTER_FALLBACK_POOL) >= 2
    for model in DEFAULT_OPENROUTER_FALLBACK_POOL:
        assert provider_of(model) == "openrouter"
