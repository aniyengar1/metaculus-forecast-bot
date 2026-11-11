"""
Prediction-market matching for the research step: searches Polymarket and
Kalshi's public, read-only, no-API-key endpoints for a market that appears
to be asking the same question we're forecasting, and -- only on a
confident match -- folds its live price/volume/liquidity into the research
summary as an anchor.

Automation note (Metaculus's "no human in the loop" rule -- see
docs/PHASE1_RESEARCH.md "No human in the loop"): the search query, the
matching/scoring below, and the price lookup are all fully automated, with
no per-question human review, selection, or override anywhere in this
file. That satisfies the rule rather than violating it -- what's actually
prohibited is a human previewing an open question and hand-picking or
tuning the forecast; an identical, deterministic research-augmentation
step run the same way for every question (like the AskNews/DuckDuckGo
research step it sits next to) is a research tool, not a human in the
forecasting loop. If this module is ever changed to let a person
cherry-pick which match to use for a specific live question, that
guarantee no longer holds.

Caching: "within a run" here means within one process -- main.py starts a
fresh process per scheduled run, so a module-level cache never carries
stale data across runs. Kalshi has no cheap full-text search endpoint, so
its entire open-events catalogue (paginated, capped) is fetched once per
run and reused for every question's matching. Polymarket's public-search
endpoint takes a real query per question, so it's cached per distinct
query string instead (a bulk listing would be far too large to fetch
Kalshi-style).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from metaculus_bot import data_index

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
_KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"

# Kalshi has thousands of open events across every category; capping the
# one-time-per-run catalogue fetch keeps this polite (a handful of GETs,
# not an unbounded crawl) at the cost of missing a genuine match that only
# exists past the cap -- acceptable since we require a confident title
# match anyway, not an exhaustive search.
_KALSHI_CACHE_PAGES = 5
_KALSHI_PAGE_LIMIT = 200

_STOPWORDS = {
    "the", "a", "an", "will", "be", "is", "are", "on", "in", "at", "by", "to",
    "of", "for", "and", "or", "what", "when", "which", "who", "how", "than",
    "before", "after", "this", "that", "its", "as", "with", "from", "into",
    "over", "under", "it", "has", "have", "been", "did", "do", "does",
}

# Confidence gate: a match needs *both* real title overlap (not just a
# nearby close date) and a high enough combined score. Tuned by hand
# against a few real questions during implementation (2026-11-10), not
# fit to any labeled dataset -- revisit if false matches/misses show up in
# practice (see market_matched/market_platform logging below, which exists
# partly to let a future review check this).
_MIN_TITLE_OVERLAP = 0.25
_MIN_COMBINED_SCORE = 0.40
_TITLE_WEIGHT = 0.7
_DATE_WEIGHT = 0.3
_DATE_PROXIMITY_HORIZON_DAYS = 90.0

_LOW_LIQUIDITY_USD = 1000.0

_polymarket_cache: dict[str, list[dict]] = {}
_kalshi_cache: list[dict] | None = None


@dataclass
class MarketMatch:
    platform: str  # "polymarket" | "kalshi"
    title: str
    price_yes: float | None  # implied probability of "Yes", 0.0-1.0
    volume_24h_usd: float | None
    liquidity_usd: float | None
    close_time: datetime | None
    url: str
    match_score: float
    low_liquidity: bool

    def as_research_block(self) -> str:
        price_str = f"{self.price_yes * 100:.1f}%" if self.price_yes is not None else "unknown"
        vol_str = f"${self.volume_24h_usd:,.0f}" if self.volume_24h_usd is not None else "unknown"
        liq_str = f"${self.liquidity_usd:,.0f}" if self.liquidity_usd is not None else "unknown"
        close_str = self.close_time.strftime("%Y-%m-%d") if self.close_time else "unknown"
        liquidity_note = (
            "LOW LIQUIDITY -- weak signal, weight accordingly"
            if self.low_liquidity
            else "liquid -- treat as a strong anchor, deviate only with a stated reason"
        )
        return (
            f"[Prediction market match: {self.platform} (match score {self.match_score:.2f}) -- "
            f'"{self.title}" -- Yes price {price_str} (implied probability), 24h volume {vol_str}, '
            f"liquidity {liq_str}, closes {close_str}, {liquidity_note}. {self.url}]"
        )


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _title_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _date_proximity(a: datetime | None, b: datetime | None) -> float:
    """1.0 for the same day, tapering linearly to 0.0 at
    _DATE_PROXIMITY_HORIZON_DAYS apart; 0.5 (neutral) if either date is
    unknown, so a missing date never by itself sinks or carries a match."""
    if a is None or b is None:
        return 0.5
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    days = abs((a - b).days)
    return max(0.0, 1.0 - days / _DATE_PROXIMITY_HORIZON_DAYS)


def _combined_score(title_overlap: float, date_score: float) -> float:
    return _TITLE_WEIGHT * title_overlap + _DATE_WEIGHT * date_score


def _build_query(question_text: str) -> str:
    """Polymarket's search ranks on relevance already -- a short, cleaned
    query works better than dumping the whole question text (which often
    includes boilerplate like 'as of November 2026')."""
    cleaned = re.sub(r"[?!.,]", "", question_text or "").strip()
    words = cleaned.split()
    return " ".join(words[:12])


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _polymarket_candidates(query: str) -> list[dict]:
    cache_key = query.strip().lower()
    if cache_key in _polymarket_cache:
        return _polymarket_cache[cache_key]
    try:
        resp = requests.get(
            _POLYMARKET_SEARCH_URL,
            params={"q": query, "limit_per_type": 8},
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception as e:
        logger.warning(f"Polymarket search failed for query {query!r}: {e}")
        events = []
    markets = [m for ev in events for m in ev.get("markets", [])]
    _polymarket_cache[cache_key] = markets
    return markets


def _kalshi_all_open_markets() -> list[dict]:
    global _kalshi_cache
    if _kalshi_cache is not None:
        return _kalshi_cache
    markets: list[dict] = []
    cursor = ""
    for _ in range(_KALSHI_CACHE_PAGES):
        params: dict[str, object] = {
            "limit": _KALSHI_PAGE_LIMIT,
            "status": "open",
            "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(_KALSHI_EVENTS_URL, params=params, timeout=_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Kalshi events fetch failed: {e}")
            break
        for event in data.get("events", []):
            for market in event.get("markets", []):
                market["_event_title"] = event.get("title", "")
                markets.append(market)
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    _kalshi_cache = markets
    return markets


def _parse_polymarket_market(raw: dict) -> tuple[str, float | None, float | None, float | None, datetime | None, str] | None:
    if raw.get("closed"):
        return None
    title = raw.get("question") or ""
    if not title:
        return None

    price_yes = None
    try:
        import json as _json

        outcomes = _json.loads(raw.get("outcomes") or "[]")
        prices = _json.loads(raw.get("outcomePrices") or "[]")
        if "Yes" in outcomes:
            price_yes = float(prices[outcomes.index("Yes")])
        elif prices:
            price_yes = float(prices[0])  # non-binary market -- best-effort
    except Exception:
        pass

    volume = _parse_float(raw.get("volume24hr"))
    liquidity = _parse_float(raw.get("liquidity"))
    close_time = _parse_iso(raw.get("endDate"))
    slug = raw.get("slug") or ""
    url = f"https://polymarket.com/event/{slug}"
    return title, price_yes, volume, liquidity, close_time, url


def _parse_kalshi_market(raw: dict) -> tuple[str, float | None, float | None, float | None, datetime | None, str] | None:
    if raw.get("status") not in (None, "active", "open"):
        return None
    title = raw.get("title") or raw.get("_event_title") or ""
    if not title:
        return None

    price_yes = _parse_float(raw.get("last_price_dollars"))
    if price_yes is None:
        bid, ask = _parse_float(raw.get("yes_bid_dollars")), _parse_float(raw.get("yes_ask_dollars"))
        if bid is not None and ask is not None:
            price_yes = (bid + ask) / 2

    # Kalshi contracts have $1 notional (see notional_value_dollars in a
    # real market payload), so contract-count volume/open-interest is a
    # reasonable dollar-volume/liquidity approximation, not an exact one --
    # good enough for the "low liquidity" soft flag this feeds into.
    volume = _parse_float(raw.get("volume_24h_fp"))
    liquidity = _parse_float(raw.get("open_interest_fp"))
    close_time = _parse_iso(raw.get("close_time"))
    ticker = raw.get("ticker") or ""
    url = f"https://kalshi.com/markets/{ticker}"
    return title, price_yes, volume, liquidity, close_time, url


def _best_match(
    platform: str,
    candidates: list[dict],
    q_tokens: set[str],
    q_close_time: datetime | None,
    parse_fn,
) -> MarketMatch | None:
    best = None
    best_score = 0.0
    for raw in candidates:
        try:
            parsed = parse_fn(raw)
        except Exception as e:
            logger.warning(f"Failed to parse a {platform} market, skipping it: {e}")
            continue
        if parsed is None:
            continue
        title, price_yes, volume, liquidity, close_time, url = parsed

        overlap = _title_overlap(q_tokens, _tokenize(title))
        if overlap < _MIN_TITLE_OVERLAP:
            continue
        score = _combined_score(overlap, _date_proximity(q_close_time, close_time))
        if score > best_score:
            best_score = score
            best = (title, price_yes, volume, liquidity, close_time, url)

    if best is None or best_score < _MIN_COMBINED_SCORE:
        return None
    title, price_yes, volume, liquidity, close_time, url = best
    low_liquidity = (liquidity is not None and liquidity < _LOW_LIQUIDITY_USD) or (
        volume is not None and volume < _LOW_LIQUIDITY_USD
    )
    return MarketMatch(
        platform=platform,
        title=title,
        price_yes=price_yes,
        volume_24h_usd=volume,
        liquidity_usd=liquidity,
        close_time=close_time,
        url=url,
        match_score=best_score,
        low_liquidity=low_liquidity,
    )


def should_skip_market_matching(question_text: str, resolution_criteria: str | None) -> bool:
    """True for questions where a betting-market search can't plausibly
    help: a pure data-index question (ENSO/ONI, EIA gas price, ...) already
    has an exact, authoritative, free number via metaculus_bot.data_index --
    search traffic spent looking for a prediction market asking the same
    thing is traffic that doesn't do anything a direct data fetch didn't
    already do better. Reuses data_index's own registry rather than keeping
    a second, separately-maintained keyword list in sync with it."""
    return data_index.find_matching_series(question_text, resolution_criteria) is not None


def find_market_matches(
    question_text: str,
    resolution_criteria: str | None,
    close_time: datetime | None,
) -> list[MarketMatch]:
    """Returns 0-2 matches: the single best Polymarket market and the
    single best Kalshi market, each only included if it clears the
    confidence gate (_MIN_TITLE_OVERLAP and _MIN_COMBINED_SCORE) -- never
    more than one per platform, and never a match on topic/date proximity
    alone without real title overlap."""
    q_tokens = _tokenize(f"{question_text} {resolution_criteria or ''}")
    query = _build_query(question_text)

    matches: list[MarketMatch] = []
    polymarket_match = _best_match(
        "polymarket", _polymarket_candidates(query), q_tokens, close_time, _parse_polymarket_market
    )
    if polymarket_match:
        matches.append(polymarket_match)
    kalshi_match = _best_match(
        "kalshi", _kalshi_all_open_markets(), q_tokens, close_time, _parse_kalshi_market
    )
    if kalshi_match:
        matches.append(kalshi_match)
    return matches
