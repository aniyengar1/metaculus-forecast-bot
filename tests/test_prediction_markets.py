"""Tests for metaculus_bot/prediction_markets.py. Deliberately does NOT hit
the live Polymarket/Kalshi endpoints in CI (slow, flaky, burns real
requests every run) -- requests.get is monkeypatched everywhere a network
call would otherwise happen. The real endpoints were hand-verified against
the live services while building this (Polymarket's public-search, Kalshi's
/events?with_nested_markets=true) -- see docs/STAGE_B_PLAN.md."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from metaculus_bot import prediction_markets as pm


@pytest.fixture(autouse=True)
def _clear_caches():
    """Each test starts with a clean module-level cache -- these caches are
    deliberately process-lifetime (see the module docstring), so tests must
    reset them or an earlier test's monkeypatched response would leak into
    a later one."""
    pm._polymarket_cache.clear()
    pm._kalshi_cache = None
    yield
    pm._polymarket_cache.clear()
    pm._kalshi_cache = None


class TestTokenizeAndOverlap:
    def test_tokenize_drops_stopwords_and_short_tokens(self):
        tokens = pm._tokenize("Will the US gas price be above $5 on a given day?")
        assert "will" not in tokens
        assert "the" not in tokens
        assert "us" not in tokens  # len < 3
        assert "gas" in tokens
        assert "price" in tokens

    def test_overlap_is_jaccard(self):
        a = {"gas", "price", "november"}
        b = {"gas", "price", "december"}
        assert pm._title_overlap(a, b) == pytest.approx(2 / 4)

    def test_overlap_zero_for_disjoint_sets(self):
        assert pm._title_overlap({"gas"}, {"election"}) == 0.0

    def test_overlap_zero_for_empty_set(self):
        assert pm._title_overlap(set(), {"gas"}) == 0.0


class TestDateProximity:
    def test_same_day_is_perfect(self):
        d = datetime(2026, 11, 10, tzinfo=timezone.utc)
        assert pm._date_proximity(d, d) == 1.0

    def test_far_apart_is_zero(self):
        a = datetime(2026, 1, 1, tzinfo=timezone.utc)
        b = a + timedelta(days=200)
        assert pm._date_proximity(a, b) == 0.0

    def test_missing_date_is_neutral(self):
        assert pm._date_proximity(None, datetime.now(timezone.utc)) == 0.5
        assert pm._date_proximity(datetime.now(timezone.utc), None) == 0.5


class TestShouldSkipMarketMatching:
    def test_skips_data_index_questions(self):
        assert pm.should_skip_market_matching(
            "Will the August-October 2026 El Niño index be 2.10 degC or higher?", None
        ) is True

    def test_does_not_skip_ordinary_questions(self):
        assert pm.should_skip_market_matching("Will the 2026 World Series require a Game 6?", None) is False


class TestParsePolymarketMarket:
    def test_parses_a_normal_binary_market(self):
        raw = {
            "question": "Will the US national average gas price exceed $4 on Nov 10 2026?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.62", "0.38"]',
            "volume24hr": "15000.5",
            "liquidity": "5000.0",
            "endDate": "2026-11-10T00:00:00Z",
            "slug": "gas-price-above-4",
            "closed": False,
        }
        parsed = pm._parse_polymarket_market(raw)
        assert parsed is not None
        title, price_yes, volume, liquidity, close_time, url = parsed
        assert "gas price" in title.lower()
        assert price_yes == pytest.approx(0.62)
        assert volume == pytest.approx(15000.5)
        assert liquidity == pytest.approx(5000.0)
        assert close_time == datetime(2026, 11, 10, tzinfo=timezone.utc)
        assert "gas-price-above-4" in url

    def test_closed_market_is_excluded(self):
        raw = {"question": "x", "outcomes": "[]", "outcomePrices": "[]", "closed": True}
        assert pm._parse_polymarket_market(raw) is None

    def test_missing_question_text_is_excluded(self):
        raw = {"question": "", "closed": False}
        assert pm._parse_polymarket_market(raw) is None


class TestParseKalshiMarket:
    def test_parses_a_normal_binary_market(self):
        raw = {
            "status": "active",
            "title": "Will Elon Musk visit Mars before Aug 1, 2099?",
            "last_price_dollars": "0.0900",
            "volume_24h_fp": "46.00",
            "open_interest_fp": "40207.75",
            "close_time": "2099-08-01T04:59:00Z",
            "ticker": "KXELONMARS-99",
        }
        parsed = pm._parse_kalshi_market(raw)
        assert parsed is not None
        title, price_yes, volume, liquidity, close_time, url = parsed
        assert "Mars" in title
        assert price_yes == pytest.approx(0.09)
        assert volume == pytest.approx(46.0)
        assert liquidity == pytest.approx(40207.75)
        assert "KXELONMARS-99" in url

    def test_falls_back_to_bid_ask_midpoint_when_no_last_price(self):
        raw = {
            "status": "active",
            "title": "Some market",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.44",
            "ticker": "X",
        }
        parsed = pm._parse_kalshi_market(raw)
        assert parsed is not None
        assert parsed[1] == pytest.approx(0.42)

    def test_falls_back_to_event_title_when_market_has_none(self):
        raw = {"status": "active", "_event_title": "Event title here", "ticker": "X"}
        parsed = pm._parse_kalshi_market(raw)
        assert parsed is not None
        assert parsed[0] == "Event title here"

    def test_inactive_market_is_excluded(self):
        raw = {"status": "settled", "title": "x", "ticker": "X"}
        assert pm._parse_kalshi_market(raw) is None


class TestBestMatch:
    def _parse_fn(self, raw):
        return raw["title"], raw["price"], raw["volume"], raw["liquidity"], raw["close"], raw["url"]

    def test_requires_minimum_title_overlap(self):
        candidates = [
            {
                "title": "Completely unrelated topic about sports",
                "price": 0.5,
                "volume": 10000,
                "liquidity": 10000,
                "close": None,
                "url": "http://x",
            }
        ]
        q_tokens = pm._tokenize("Will the US gas price exceed four dollars")
        assert pm._best_match("test", candidates, q_tokens, None, self._parse_fn) is None

    def test_confident_match_is_returned(self):
        candidates = [
            {
                "title": "Will the US national average gas price exceed $4 in November 2026?",
                "price": 0.62,
                "volume": 15000,
                "liquidity": 5000,
                "close": datetime(2026, 11, 10, tzinfo=timezone.utc),
                "url": "http://polymarket.example/x",
            }
        ]
        q_tokens = pm._tokenize("Will the US national average gas price exceed $4 on Nov 10 2026?")
        q_close = datetime(2026, 11, 10, tzinfo=timezone.utc)
        match = pm._best_match("polymarket", candidates, q_tokens, q_close, self._parse_fn)
        assert match is not None
        assert match.platform == "polymarket"
        assert match.price_yes == 0.62
        assert match.low_liquidity is False

    def test_low_liquidity_market_is_flagged(self):
        candidates = [
            {
                "title": "Will the US national average gas price exceed $4 in November 2026?",
                "price": 0.62,
                "volume": 50,
                "liquidity": 80,
                "close": None,
                "url": "http://x",
            }
        ]
        q_tokens = pm._tokenize("Will the US national average gas price exceed $4 on Nov 10 2026?")
        match = pm._best_match("polymarket", candidates, q_tokens, None, self._parse_fn)
        assert match is not None
        assert match.low_liquidity is True

    def test_picks_highest_scoring_candidate(self):
        candidates = [
            {
                "title": "Gas price something vaguely related",
                "price": 0.1,
                "volume": 10000,
                "liquidity": 10000,
                "close": None,
                "url": "http://weak",
            },
            {
                "title": "Will the US national average gas price exceed $4 in November 2026?",
                "price": 0.62,
                "volume": 10000,
                "liquidity": 10000,
                "close": None,
                "url": "http://strong",
            },
        ]
        q_tokens = pm._tokenize("Will the US national average gas price exceed $4 on Nov 10 2026?")
        match = pm._best_match("polymarket", candidates, q_tokens, None, self._parse_fn)
        assert match is not None
        assert match.url == "http://strong"

    def test_parse_failure_on_one_candidate_does_not_block_others(self):
        def flaky_parse(raw):
            if raw.get("broken"):
                raise KeyError("boom")
            return raw["title"], raw["price"], raw["volume"], raw["liquidity"], raw["close"], raw["url"]

        candidates = [
            {"broken": True},
            {
                "title": "Will the US national average gas price exceed $4 in November 2026?",
                "price": 0.62,
                "volume": 10000,
                "liquidity": 10000,
                "close": None,
                "url": "http://ok",
            },
        ]
        q_tokens = pm._tokenize("Will the US national average gas price exceed $4 on Nov 10 2026?")
        match = pm._best_match("polymarket", candidates, q_tokens, None, flaky_parse)
        assert match is not None
        assert match.url == "http://ok"


class TestFindMarketMatches:
    def test_returns_empty_list_when_nothing_matches(self, monkeypatch):
        monkeypatch.setattr(pm, "_polymarket_candidates", lambda query: [])
        monkeypatch.setattr(pm, "_kalshi_all_open_markets", lambda: [])
        matches = pm.find_market_matches("Will the 2026 World Series require a Game 6?", None, None)
        assert matches == []

    def test_caches_polymarket_results_per_query_within_a_run(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params["q"])

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"events": []}

            return _Resp()

        monkeypatch.setattr(pm.requests, "get", fake_get)
        pm._polymarket_candidates("gas price")
        pm._polymarket_candidates("gas price")  # same query -- should hit cache, not requests.get again
        assert len(calls) == 1

    def test_caches_kalshi_results_across_calls_within_a_run(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(1)

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"events": [], "cursor": ""}

            return _Resp()

        monkeypatch.setattr(pm.requests, "get", fake_get)
        pm._kalshi_all_open_markets()
        pm._kalshi_all_open_markets()
        assert len(calls) == 1  # second call served from the module-level cache


class TestMarketMatchResearchBlock:
    def test_liquid_match_says_strong_anchor(self):
        match = pm.MarketMatch(
            platform="polymarket",
            title="Will X happen?",
            price_yes=0.62,
            volume_24h_usd=15000.0,
            liquidity_usd=5000.0,
            close_time=datetime(2026, 11, 10, tzinfo=timezone.utc),
            url="http://polymarket.example/x",
            match_score=0.75,
            low_liquidity=False,
        )
        block = match.as_research_block()
        assert "strong anchor" in block
        assert "62.0%" in block
        assert "2026-11-10" in block

    def test_low_liquidity_match_says_weak_signal(self):
        match = pm.MarketMatch(
            platform="kalshi",
            title="Will Y happen?",
            price_yes=0.3,
            volume_24h_usd=20.0,
            liquidity_usd=50.0,
            close_time=None,
            url="http://kalshi.example/y",
            match_score=0.5,
            low_liquidity=True,
        )
        block = match.as_research_block()
        assert "LOW LIQUIDITY" in block
        assert "weak signal" in block
