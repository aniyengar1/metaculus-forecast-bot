"""
Reports how many of our submitted forecasts' questions are scheduled to
resolve within upcoming horizons, surfaced in the weekly summary.
Read-only: GETs against the Metaculus API only, never a submission.

Usage:
    poetry run python scripts/upcoming_resolutions.py [--db-path data/forecasts.db]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metaculus_bot import db  # noqa: E402

import performance_report  # noqa: E402

_HORIZON_DAYS = [7, 14, 30, 60, 90]


def extract_scheduled_resolve(post_json: dict[str, Any], question_id: int) -> str | None:
    """scheduled_resolve_time (ISO string) for this specific question_id --
    handles both a plain post (top-level 'question') and a group-of-questions
    post. Group subquestions each have their OWN scheduled_resolve_time
    under group_of_questions.questions[]; the post's own top-level
    scheduled_resolve_time is just one arbitrary subquestion's value (in
    practice the last one) and is wrong for any other subquestion in the
    same group -- confirmed 2026-10-16 against post 43325, a 4-subquestion
    group where our two forecasted subquestions resolve two years apart."""
    if "question" in post_json:
        return post_json["question"].get("scheduled_resolve_time")
    for q in post_json.get("group_of_questions", {}).get("questions", []):
        if q.get("id") == question_id:
            return q.get("scheduled_resolve_time")
    return None


def generate_upcoming_resolutions_report(
    conn: sqlite3.Connection, token: str | None, *, now: datetime | None = None
) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["\n=== Upcoming resolutions ===\n"]

    if not token:
        lines.append("  No METACULUS_TOKEN available -- skipping.")
        return "\n".join(lines)

    rows = db.latest_forecasts_per_question(conn, dry_run=False)
    resolve_times: list[datetime] = []
    for row in rows:
        qjson = performance_report.fetch_question_json(row["post_id"], token)
        if qjson is None:
            continue
        sched = extract_scheduled_resolve(qjson, row["question_id"])
        if sched is None:
            continue
        try:
            resolve_times.append(datetime.strptime(sched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            continue

    if not resolve_times:
        lines.append("  No submitted forecasts with a known scheduled resolve date.")
        return "\n".join(lines)

    total = len(resolve_times)
    already = sum(1 for t in resolve_times if t <= now)
    lines.append(f"  {total} submitted forecasts tracked ({already} already past their scheduled resolve time).")
    for horizon in _HORIZON_DAYS:
        cutoff = now + timedelta(days=horizon)
        count = sum(1 for t in resolve_times if t <= cutoff)
        lines.append(f"  Resolved by +{horizon}d ({cutoff.strftime('%Y-%m-%d')}): {count}/{total}")

    return "\n".join(lines)


if __name__ == "__main__":
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Print the upcoming-resolutions report")
    parser.add_argument("--db-path", type=str, default="data/forecasts.db")
    args = parser.parse_args()

    with db.connect(args.db_path) as conn:
        print(generate_upcoming_resolutions_report(conn, os.getenv("METACULUS_TOKEN")))
