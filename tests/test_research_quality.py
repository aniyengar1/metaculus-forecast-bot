"""Tests for metaculus_bot/research_quality.py -- the cheap, deterministic
guard that shrinks/widens a final forecast when the research behind it was
weak (empty, very short, a known fallback-chain failure phrase, or no dated
source). See the 2026-11-09 review: the El Nino question (worst loss of the
season) had all three ensemble models confidently agree on the wrong side
with nothing dated backing them up -- this guard exists to bound exactly
that failure mode."""
from __future__ import annotations

from datetime import datetime, timezone

from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile, PredictedOption, PredictedOptionList

from metaculus_bot.research_quality import (
    assess_research_quality,
    flatten_mc_toward_uniform,
    shrink_binary_toward_half,
    widen_numeric_distribution,
)

GAS_PRICE_QUESTION = NumericQuestion(
    id_of_question=46142,
    id_of_post=45960,
    question_text="What will the US national average gas price be?",
    page_url="https://www.metaculus.com/questions/45960",
    upper_bound=6.0,
    lower_bound=3.5,
    open_upper_bound=True,
    open_lower_bound=True,
    zero_point=None,
)

DATE_QUESTION = DateQuestion(
    id_of_question=43326,
    id_of_post=43324,
    question_text="When will martial law be lifted in at least 3/4 of Ukraine?",
    page_url="https://www.metaculus.com/questions/43324",
    upper_bound=datetime(2027, 5, 28, 21, 0, tzinfo=timezone.utc),
    lower_bound=datetime(2022, 5, 29, tzinfo=timezone.utc),
    open_upper_bound=True,
    open_lower_bound=False,
    zero_point=None,
)


def _numeric_distribution(percentiles: list[Percentile]) -> NumericDistribution:
    return NumericDistribution.from_question(percentiles, GAS_PRICE_QUESTION)


class TestAssessResearchQuality:
    def test_empty_is_weak(self):
        quality, reason = assess_research_quality("")
        assert quality == "weak"
        assert "empty" in reason

    def test_only_source_tag_is_weak(self):
        quality, _ = assess_research_quality("[research source: duckduckgo_fallback]\n")
        assert quality == "weak"

    def test_very_short_is_weak(self):
        quality, reason = assess_research_quality(
            "[research source: duckduckgo_fallback]\nNothing much found about this."
        )
        assert quality == "weak"
        assert "short" in reason

    def test_known_failure_phrase_is_weak(self):
        text = (
            "[research source: asknews]\n"
            "No recent AskNews articles found. " * 3
        )
        quality, reason = assess_research_quality(text)
        assert quality == "weak"
        assert "no results" in reason

    def test_no_dated_source_is_weak(self):
        body = (
            "Gas prices are influenced by crude oil costs, refining capacity, "
            "seasonal demand shifts, and broader macroeconomic trends that "
            "vary across regions and time of year without a specific anchor."
        )
        quality, reason = assess_research_quality(f"[research source: duckduckgo_fallback]\n{body}")
        assert quality == "weak"
        assert "dated" in reason

    def test_substantive_dated_research_is_ok(self):
        body = (
            "EIA reported the US national average gas price at $4.153/gal "
            "for the week of 2026-11-02, up from $4.084/gal a month earlier "
            "amid refinery maintenance season and firmer crude benchmarks."
        )
        quality, reason = assess_research_quality(f"[research source: asknews]\n{body}")
        assert quality == "ok"
        assert "substantive" in reason


class TestShrinkBinaryTowardHalf:
    def test_shrinks_high_confidence_toward_half(self):
        shrunk = shrink_binary_toward_half(0.95)
        assert 0.5 < shrunk < 0.95

    def test_shrinks_low_confidence_toward_half(self):
        shrunk = shrink_binary_toward_half(0.05)
        assert 0.05 < shrunk < 0.5

    def test_clips_to_guard_bounds(self):
        assert shrink_binary_toward_half(0.999) <= 0.85
        assert shrink_binary_toward_half(0.001) >= 0.15

    def test_half_is_a_fixed_point(self):
        assert abs(shrink_binary_toward_half(0.5) - 0.5) < 1e-9


class TestFlattenMcTowardUniform:
    def test_moves_toward_uniform_and_still_sums_to_one(self):
        options = PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name="A", probability=0.85),
                PredictedOption(option_name="B", probability=0.10),
                PredictedOption(option_name="C", probability=0.05),
            ]
        )
        flattened = flatten_mc_toward_uniform(options)
        probs = {o.option_name: o.probability for o in flattened.predicted_options}
        assert abs(sum(probs.values()) - 1.0) < 1e-9
        # Moved halfway toward 1/3 each, so A should have dropped and C risen.
        assert probs["A"] < 0.85
        assert probs["C"] > 0.05
        assert abs(probs["A"] - (0.5 * 0.85 + 0.5 / 3)) < 1e-9


class TestWidenNumericDistribution:
    def test_widens_p10_p90_band(self):
        original = _numeric_distribution(
            [
                Percentile(percentile=0.1, value=4.0),
                Percentile(percentile=0.5, value=4.2),
                Percentile(percentile=0.9, value=4.4),
            ]
        )
        widened = widen_numeric_distribution(original, GAS_PRICE_QUESTION)

        orig_p10, orig_p90 = (p.value for p in original.get_percentiles_at_target_heights([0.1, 0.9]))
        new_p10, new_p90 = (p.value for p in widened.get_percentiles_at_target_heights([0.1, 0.9]))
        assert (new_p90 - new_p10) > (orig_p90 - orig_p10)

    def test_stays_within_question_bounds(self):
        # Already near-full-range -- widening by 2x must clip to the bounds
        # rather than overshoot them.
        original = _numeric_distribution(
            [
                Percentile(percentile=0.1, value=3.6),
                Percentile(percentile=0.5, value=4.5),
                Percentile(percentile=0.9, value=5.9),
            ]
        )
        widened = widen_numeric_distribution(original, GAS_PRICE_QUESTION)
        p10, p90 = (p.value for p in widened.get_percentiles_at_target_heights([0.1, 0.9]))
        assert p10 >= GAS_PRICE_QUESTION.lower_bound - 1e-6
        assert p90 <= GAS_PRICE_QUESTION.upper_bound + 1e-6

    def test_works_for_date_questions(self):
        def _ts(date_str: str) -> float:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()

        original = NumericDistribution.from_question(
            [
                Percentile(percentile=0.1, value=_ts("2026-11-01")),
                Percentile(percentile=0.5, value=_ts("2027-02-01")),
                Percentile(percentile=0.9, value=_ts("2027-05-20")),
            ],
            DATE_QUESTION,
        )
        widened = widen_numeric_distribution(original, DATE_QUESTION)
        orig_p10, orig_p90 = (p.value for p in original.get_percentiles_at_target_heights([0.1, 0.9]))
        new_p10, new_p90 = (p.value for p in widened.get_percentiles_at_target_heights([0.1, 0.9]))
        assert (new_p90 - new_p10) > (orig_p90 - orig_p10)
        assert new_p10 >= DATE_QUESTION.lower_bound.timestamp() - 1e-6
        assert new_p90 <= DATE_QUESTION.upper_bound.timestamp() + 1e-6
