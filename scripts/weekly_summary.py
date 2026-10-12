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


def _tournament_section(conn: sqlite3.Connection, since: str, tournament: str | None) -> list[str]:
    """tournament=None means "all tournaments combined"; otherwise filters
    to that one label (seasonal/minibench/test/manual -- see main.py's
    tournament_label)."""
    lines: list[str] = []
    tournament_clause = "" if tournament is None else "AND tournament = ?"
    params: tuple = (since,) if tournament is None else (since, tournament)

    rows = conn.execute(
        f"SELECT status, dry_run, COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(estimated_cost_usd), 0) "
        f"FROM forecasts WHERE run_timestamp >= ? {tournament_clause} GROUP BY status, dry_run",
        params,
    ).fetchall()
    total_ok = sum(r[2] for r in rows if r[0] == "ok")
    total_cost = sum(r[3] for r in rows if r[0] == "ok")
    total_estimated_cost = sum(r[4] for r in rows if r[0] == "ok")
    total_skipped_budget = sum(r[2] for r in rows if r[0] == "skipped_budget")
    total_errors = sum(r[2] for r in rows if r[0] == "error")
    real_submitted = sum(r[2] for r in rows if r[0] == "ok" and r[1] == 0)
    dry_run_count = sum(r[2] for r in rows if r[0] == "ok" and r[1] == 1)

    if total_ok == 0 and total_skipped_budget == 0 and total_errors == 0:
        lines.append("  (no activity this window)")
        return lines

    lines.append(f"  Questions forecast (ok): {total_ok}  (real submissions: {real_submitted}, dry-run: {dry_run_count})")
    lines.append(f"  Skipped (budget guard):  {total_skipped_budget}")
    lines.append(f"  Errored:                 {total_errors}")
    lines.append(f"  Real $ spent:            ${total_cost:.4f}")
    if abs(total_estimated_cost - total_cost) > 1e-9:
        lines.append(
            f"  (litellm-estimated:      ${total_estimated_cost:.4f} -- higher because it doesn't "
            "know Groq/Gemini are free-tier here; real $ spent is what the budget guard uses)"
        )

    by_type = conn.execute(
        f"SELECT question_type, COUNT(*) FROM forecasts "
        f"WHERE run_timestamp >= ? AND status = 'ok' {tournament_clause} GROUP BY question_type",
        params,
    ).fetchall()
    if by_type:
        lines.append("  By question type:")
        for qtype, count in by_type:
            lines.append(f"    {qtype:<22} {count}")
    return lines


def summarize(conn: sqlite3.Connection, days: int) -> str:
    since = _window_start(days)
    lines: list[str] = []
    lines.append(f"=== Weekly summary (last {days} days) ===\n")

    lines.append("--- All tournaments combined ---")
    lines.extend(_tournament_section(conn, since, tournament=None))

    known_tournaments = conn.execute(
        "SELECT DISTINCT tournament FROM forecasts WHERE run_timestamp >= ? AND tournament IS NOT NULL",
        (since,),
    ).fetchall()
    for (tournament,) in sorted(known_tournaments):
        lines.append(f"\n--- {tournament} ---")
        lines.extend(_tournament_section(conn, since, tournament))

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
