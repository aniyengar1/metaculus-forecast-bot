"""
Backtest for metaculus_bot/research_quality.py: for every resolved binary
question we've logged, replay the guard against the research_summary and
post_calibration_value we actually stored at forecast time, and report what
the Brier score would have been with vs without it. Pure counterfactual --
never submits anything, never calls an LLM, never calls Metaculus except the
same read-only GET scripts/performance_report.py already uses.

We can't recompute Metaculus's own peer score offline (it's relative to the
whole field of forecasters, not a function of our prediction + outcome
alone), so this reports Brier score, which the guard directly operates on
and peer score is monotonic in. For multiple choice we report the
probability-mass-on-the-actual-outcome instead (log-loss's natural analog
when there's no single "correct probability"). For numeric/date we don't
have a scalar score to replay the guard against post-hoc, so we instead
report whether the resolution falls inside the widened P10-P90 band (it
already fell inside the original dense aggregate by construction unless the
sanity guard rejected the forecast) -- see docs/STAGE_B_PLAN.md "Research
quality guard backtest" for the full writeup.

Usage:
    poetry run python scripts/backtest_research_quality_guard.py [--db-path data/forecasts.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import performance_report as pr  # noqa: E402
from performance_report import brier_score, fetch_question_json, is_resolved, parse_binary_outcome  # noqa: E402

from metaculus_bot import db  # noqa: E402
from metaculus_bot.research_quality import assess_research_quality, shrink_binary_toward_half  # noqa: E402

# This script can touch several dozen distinct posts in one run (one per
# resolved binary question we've ever forecast) -- pace more conservatively
# than performance_report.py's own 0.5s default to avoid a long run of 429s
# against Metaculus's rate limiter (confirmed necessary in practice;
# performance_report.py's default spacing alone wasn't enough for a batch
# this size on 2026-11-09).
pr._MIN_REQUEST_INTERVAL_SECONDS = 2.0
pr._RETRY_AFTER_429_SECONDS = 8


def _parse_logged_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        unwrapped = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if unwrapped is None:
        return None
    try:
        return float(unwrapped)
    except (TypeError, ValueError):
        return None


def run_backtest(conn: sqlite3.Connection, token: str) -> str:
    lines: list[str] = ["=== Research-quality guard backtest (binary questions) ===\n"]

    rows = [
        r
        for r in db.latest_forecasts_per_question(conn, dry_run=None)
        if r["question_type"] == "BinaryQuestion"
    ]

    results = []
    for row in rows:
        qjson = fetch_question_json(row["post_id"], token)
        if qjson is None or not is_resolved(qjson):
            continue
        outcome = parse_binary_outcome(qjson)
        if outcome is None:
            continue
        original_prob = _parse_logged_float(row["post_calibration_value"])
        if original_prob is None:
            continue

        quality, reason = assess_research_quality(row["research_summary"] or "")
        counterfactual_prob = (
            shrink_binary_toward_half(original_prob) if quality == "weak" else original_prob
        )

        original_brier = brier_score(original_prob, outcome)
        counterfactual_brier = brier_score(counterfactual_prob, outcome)
        results.append(
            {
                "post_id": row["post_id"],
                "title": (row["question_title"] or "")[:70],
                "quality": quality,
                "reason": reason,
                "outcome": outcome,
                "original_prob": original_prob,
                "counterfactual_prob": counterfactual_prob,
                "original_brier": original_brier,
                "counterfactual_brier": counterfactual_brier,
                "delta": original_brier - counterfactual_brier,
            }
        )

    if not results:
        lines.append("No resolved binary questions with logged research found.")
        return "\n".join(lines)

    results.sort(key=lambda r: r["delta"], reverse=True)
    fired = [r for r in results if r["quality"] == "weak"]
    lines.append(f"Resolved binary questions checked: {len(results)}  (guard would have fired on {len(fired)})\n")

    orig_total = sum(r["original_brier"] for r in results)
    cf_total = sum(r["counterfactual_brier"] for r in results)
    lines.append(
        f"Mean Brier, as actually submitted:        {orig_total / len(results):.4f}\n"
        f"Mean Brier, with the guard replayed:      {cf_total / len(results):.4f}\n"
    )

    lines.append("Per-question (sorted by Brier improvement from the guard, best first):")
    lines.append(
        f"  {'post_id':<8} {'fired':<6} {'outcome':<8} {'orig_p':<8} {'guard_p':<8} "
        f"{'orig_brier':<11} {'guard_brier':<12} {'delta':<8} title"
    )
    for r in results:
        fired_mark = "yes" if r["quality"] == "weak" else "no"
        lines.append(
            f"  {r['post_id']:<8} {fired_mark:<6} {r['outcome']:<8.0f} {r['original_prob']:<8.3f} "
            f"{r['counterfactual_prob']:<8.3f} {r['original_brier']:<11.4f} {r['counterfactual_brier']:<12.4f} "
            f"{r['delta']:+.4f}  {r['title']}"
        )
        if r["quality"] == "weak":
            lines.append(f"           (guard reason: {r['reason']})")

    return "\n".join(lines)


if __name__ == "__main__":
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Backtest the research-quality guard against resolved questions")
    parser.add_argument("--db-path", type=str, default="data/forecasts.db")
    args = parser.parse_args()

    with db.connect(args.db_path) as conn:
        print(run_backtest(conn, os.getenv("METACULUS_TOKEN")))
