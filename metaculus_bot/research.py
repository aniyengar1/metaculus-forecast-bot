"""
Research step: AskNews "latest news" calls only (never the 5x-cost historical
/archive endpoint), capped per-question and tracked monthly in SQLite against
the 1k/month tournament cap. Falls back to a free DuckDuckGo text search (no
API key required) when AskNews creds are missing or the monthly cap is close,
then summarizes whichever raw research into a short structured brief via LLM.

forecasting_tools' own AskNewsSearcher always calls both the "latest news" AND
"news knowledge" (historical/archive, 5x call cost) endpoints per query -- that
is exactly the expensive path we've been told to avoid, so this module talks
to the asknews_sdk directly instead of reusing that class.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

from forecasting_tools import GeneralLlm, MetaculusQuestion, clean_indents

from metaculus_bot import db
from metaculus_bot.config import Config, asknews_creds_present

logger = logging.getLogger(__name__)

ASKNEWS_RATE_LIMIT_SECONDS = 10  # free tier: ~1 call per 10s


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def _asknews_latest_news(query: str, n_articles: int = 6) -> str:
    from asknews_sdk import AsyncAskNewsSDK

    client_id = os.getenv("ASKNEWS_CLIENT_ID")
    client_secret = os.getenv("ASKNEWS_SECRET")
    api_key = os.getenv("ASKNEWS_API_KEY")

    async with AsyncAskNewsSDK(
        client_id=client_id,
        client_secret=client_secret,
        api_key=api_key,
        scopes={"news"},
    ) as ask:
        response = await ask.news.search_news(
            query=query,
            n_articles=n_articles,
            return_type="both",
            strategy="latest news",  # recent-news only -- never "news knowledge" (archive, 5x cost)
        )

    articles = response.as_dicts or []
    if not articles:
        return "No recent AskNews articles found."
    sorted_articles = sorted(articles, key=lambda a: a.pub_date, reverse=True)
    lines = []
    for a in sorted_articles:
        pub_date = a.pub_date.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"**{a.eng_title}**\n{a.summary}\nPublished: {pub_date} | Source: {a.article_url}")
    return "AskNews (latest news, last 48h):\n\n" + "\n\n".join(lines)


async def _duckduckgo_fallback(query: str, max_results: int = 6) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        return "No research available (ddgs package not installed)."

    def _search() -> list[dict]:
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            logger.warning(f"DuckDuckGo fallback search failed: {e}")
            return []

    results = await asyncio.to_thread(_search)
    if not results:
        return "No web search results found (free fallback source)."
    lines = [
        f"- {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})"
        for r in results
    ]
    return "Web search results (DuckDuckGo, free fallback -- no AskNews creds/budget used):\n" + "\n".join(lines)


async def _get_raw_research(
    question: MetaculusQuestion, cfg: Config, conn: sqlite3.Connection
) -> tuple[str, str]:
    """Returns (raw_research_text, source_label)."""
    if asknews_creds_present():
        month_key = _current_month_key()
        used = db.get_asknews_usage(conn, month_key)
        remaining = cfg.asknews_monthly_cap - used
        calls_allowed = min(cfg.asknews_max_calls_per_question, max(remaining, 0))
        if calls_allowed > 0:
            try:
                raw = await _asknews_latest_news(question.question_text)
                db.increment_asknews_usage(conn, month_key, 1)
                if calls_allowed > 1:
                    # Room for a second, more targeted query using the
                    # resolution criteria -- kept to recent-news only, same
                    # cap accounting.
                    await asyncio.sleep(ASKNEWS_RATE_LIMIT_SECONDS)
                    extra_query = f"{question.question_text} {question.resolution_criteria or ''}".strip()
                    try:
                        raw_extra = await _asknews_latest_news(extra_query)
                        db.increment_asknews_usage(conn, month_key, 1)
                        raw = raw + "\n\n" + raw_extra
                    except Exception as e:
                        logger.warning(f"Second AskNews call failed, continuing with first: {e}")
                return raw, "asknews"
            except Exception as e:
                logger.warning(f"AskNews call failed, falling back to free search: {e}")
        else:
            logger.info(
                f"AskNews monthly cap reached ({used}/{cfg.asknews_monthly_cap}), using fallback search"
            )
    raw = await _duckduckgo_fallback(question.question_text)
    return raw, "duckduckgo_fallback"


async def gather_research(
    question: MetaculusQuestion,
    cfg: Config,
    conn: sqlite3.Connection,
    summarizer_llm: GeneralLlm,
) -> str:
    raw, source = await _get_raw_research(question, cfg, conn)

    prompt = clean_indents(
        f"""
        Summarize the following research into a concise, structured brief (at most 200 words)
        for a forecaster answering this question:
        {question.question_text}

        Only include facts relevant to this resolution criteria:
        {question.resolution_criteria}

        Do not give your own forecast or opinion -- only summarize what the research says.

        Research:
        {raw}
        """
    )
    try:
        summary = await summarizer_llm.invoke(prompt)
    except Exception as e:
        logger.warning(f"Research summarization failed, using raw research: {e}")
        summary = raw

    return f"[research source: {source}]\n{summary}"
