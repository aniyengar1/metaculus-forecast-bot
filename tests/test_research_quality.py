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
    binary_clip_bounds,
    flatten_mc_toward_uniform,
    has_implausible_subdollar_commodity_price,
    has_no_direct_evidence,
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


class TestHasImplausibleSubdollarCommodityPrice:
    # Real research text from the 2026-11-03 USDA egg-price loss (-33.0
    # peer): the actual USDA report said 73 cents/dozen, but research read
    # "$0.35 per dozen" off the source and converted it (correctly, as a
    # dollars-to-cents conversion) to "35" -- the wrong number went in
    # before any unit conversion happened.
    _EGG_BUG_TEXT_NO_DATE = (
        "[research source: duckduckgo_fallback]\n"
        "**Research Summary:**\n"
        "* **Target Report:** USDA AMS \"Egg Markets Overview\" (Report 3725).\n"
        "* **Specific Price Data Point:** The exact sentence covering the "
        "target metric (\"Wholesale prices for national trading of truck "
        "lot quantities of graded, loose, white Large shell eggs\") is "
        "recorded in the primary source document at **$0.35 per dozen**.\n"
        "* **Resolution Value:** Expressed in cents per dozen as required "
        "by the resolution criteria, the reported price is **35**."
    )
    # Same text, but with an explicit date mentioned elsewhere -- passes
    # assess_research_quality's length/date/failure-phrase checks as "ok"
    # before this module's fix (confirmed against the pre-fix code during
    # development); this check must catch it regardless.
    _EGG_BUG_TEXT_WITH_DATE = _EGG_BUG_TEXT_NO_DATE.replace(
        "Report 3725).", "Report 3725) dated November 13, 2026."
    )

    def test_detects_the_real_egg_price_bug_text(self):
        assert has_implausible_subdollar_commodity_price(self._EGG_BUG_TEXT_NO_DATE) is True

    def test_assess_research_quality_flags_undated_version_as_weak(self):
        quality, reason = assess_research_quality(self._EGG_BUG_TEXT_NO_DATE)
        assert quality == "weak"

    def test_assess_research_quality_flags_dated_version_as_weak_too(self):
        # The key regression: a date mention alone must not be enough to
        # wave this through as "ok".
        quality, reason = assess_research_quality(self._EGG_BUG_TEXT_WITH_DATE)
        assert quality == "weak"
        assert "sub-$1" in reason

    def test_normal_dollar_amount_is_not_flagged(self):
        text = "The wholesale price for graded loose eggs was $2.15 per dozen as of November 2026."
        assert has_implausible_subdollar_commodity_price(text) is False

    def test_subdollar_amount_without_per_unit_context_is_not_flagged(self):
        # A sub-$1 figure that isn't anywhere near a per-unit word (e.g. a
        # stock price move, a tax rate) shouldn't trip this -- it's
        # specifically about implausible per-unit commodity quotes.
        text = "The company's stock rose by $0.35 after the earnings call in November 2026."
        assert has_implausible_subdollar_commodity_price(text) is False

    def test_empty_text_is_not_flagged(self):
        assert has_implausible_subdollar_commodity_price("") is False


class TestHasNoDirectEvidence:
    def test_flags_ast_style_inconclusive_research(self):
        # Real research text from the 2026-11-13 AST driving-test-calendar
        # loss (-42.2 peer): long, dated, substantive -- "ok" quality -- but
        # explicitly says it found nothing on the actual question.
        text = (
            "[research source: duckduckgo_fallback]\n"
            "AST S.p.A. initiated a selection process for hiring 73 operatori "
            "di esercizio, announced in September 2026. The provided search "
            "results confirm the existence of the selection process "
            "announcement, but contain no information regarding the "
            "subsequent publication of a practical-driving-test calendar or "
            "candidate convocations."
        )
        quality, _ = assess_research_quality(text)
        assert quality == "ok"  # long/dated enough to pass the weak check
        assert has_no_direct_evidence(text) is True

    def test_substantive_conclusive_research_has_evidence(self):
        body = (
            "EIA reported the US national average gas price at $4.153/gal "
            "for the week of 2026-11-02, up from $4.084/gal a month earlier."
        )
        assert has_no_direct_evidence(f"[research source: asknews]\n{body}") is False

    def test_empty_text_does_not_crash_and_has_no_phrase_match(self):
        # Empty research is already caught by assess_research_quality's
        # "weak" path (binary_clip_bounds ORs the two together) -- this
        # function only needs to not blow up on it.
        assert has_no_direct_evidence("") is False


class TestBinaryClipBounds:
    def test_weak_quality_uses_tight_bounds(self):
        assert binary_clip_bounds("weak", False, 0.05, 0.95) == (0.15, 0.85)

    def test_no_direct_evidence_uses_tight_bounds_even_if_ok(self):
        assert binary_clip_bounds("ok", True, 0.05, 0.95) == (0.15, 0.85)

    def test_ok_quality_with_evidence_uses_default_bounds(self):
        assert binary_clip_bounds("ok", False, 0.05, 0.95) == (0.05, 0.95)


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
