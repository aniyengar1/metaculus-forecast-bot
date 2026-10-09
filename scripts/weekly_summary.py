"""
Weekly summary of bot activity, read entirely from our own SQLite log
(data/forecasts.db) -- no Metaculus or OpenRouter API calls. Covers:
questions forecast, $ spent, AskNews calls used this month, and (once the
`resolutions` table has rows -- nothing populates it yet, that's a later
step) Brier/log score.

Usage:
    poetry run python scripts/weekly_summary.py [--db-path data/forecasts.db] [--days 7]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaculus_bot import db  # noqa: E402


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def summarize(conn: sqlite3.Connection, days: int) -> str:
    since = _window_start(days)
    lines: list[str] = []
    lines.append(f"=== Weekly summary (last {days} days) ===\n")

    rows = conn.execute(
        "SELECT status, dry_run, COUNT(*), COALESCE(SUM(cost_usd), 0) "
        "FROM forecasts WHERE run_timestamp >= ? GROUP BY status, dry_run",
        (since,),
    ).fetchall()
    total_ok = sum(r[2] for r in rows if r[0] == "ok")
    total_cost = sum(r[3] for r in rows if r[0] == "ok")
    total_skipped_budget = sum(r[2] for r in rows if r[0] == "skipped_budget")
    total_errors = sum(r[2] for r in rows if r[0] == "error")
    real_submitted = sum(r[2] for r in rows if r[0] == "ok" and r[1] == 0)
    dry_run_count = sum(r[2] for r in rows if r[0] == "ok" and r[1] == 1)

    lines.append(f"Questions forecast (ok): {total_ok}  (real submissions: {real_submitted}, dry-run: {dry_run_count})")
    lines.append(f"Skipped (budget guard):  {total_skipped_budget}")
    lines.append(f"Errored:                 {total_errors}")
    lines.append(f"Total $ spent:           ${total_cost:.4f}")

    by_type = conn.execute(
        "SELECT question_type, COUNT(*) FROM forecasts "
        "WHERE run_timestamp >= ? AND status = 'ok' GROUP BY question_type",
        (since,),
    ).fetchall()
    if by_type:
        lines.append("\nBy question type:")
        for qtype, count in by_type:
            lines.append(f"  {qtype:<22} {count}")

    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    asknews_used = db.get_asknews_usage(conn, month_key)
    lines.append(f"\nAskNews calls this month ({month_key}): {asknews_used}")

    alerts = conn.execute(
        "SELECT timestamp, message FROM alerts WHERE timestamp >= ? ORDER BY timestamp",
        (since,),
    ).fetchall()
    if alerts:
        lines.append(f"\n🚨 {len(alerts)} alert(s) this window:")
        for ts, msg in alerts:
            lines.append(f"  [{ts}] {msg}")
    else:
        lines.append("\nNo alerts this window.")

    resolved = conn.execute(
        "SELECT r.brier_score, r.log_score FROM resolutions r "
        "JOIN forecasts f ON f.question_id = r.question_id "
        "WHERE r.resolved_at >= ?",
        (since,),
    ).fetchall()
    if resolved:
        briers = [b for b, _ in resolved if b is not None]
        logs = [l for _, l in resolved if l is not None]
        lines.append(f"\nResolved questions this window: {len(resolved)}")
        if briers:
            lines.append(f"  Average Brier score: {sum(briers)/len(briers):.4f}")
        if logs:
            lines.append(f"  Average log score:   {sum(logs)/len(logs):.4f}")
    else:
        lines.append(
            "\nNo resolved questions yet to score (the `resolutions` table isn't "
            "populated by anything yet -- that's a later calibration-analysis step)."
        )

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print a weekly bot activity summary")
    parser.add_argument("--db-path", type=str, default="data/forecasts.db")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    with db.connect(args.db_path) as conn:
        print(summarize(conn, args.days))
