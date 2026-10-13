"""
Pulls Metaculus's own scores (peer + baseline) for every question we've
forecast that has since resolved, and combines them with our local log to
produce a performance report: average peer score overall/by tournament/by
question type, per-model Brier for binary questions (each model's raw,
pre-ensemble forecast vs the actual outcome), raw-ensemble-vs-post-
calibration Brier, and a 10-bin calibration table.

Read-only: every Metaculus API call here is a GET against /api/posts/{id}/
(never posts a prediction/comment, never submits anything). See
docs/STAGE_B_PLAN.md "Performance report" for how the score_data schema
below was determined -- Metaculus's REST API doesn't document it, but their
open-source webapp's docs/openapi.yml does (score_data keys: peer_score,
baseline_score, spot_peer_score, spot_baseline_score, coverage, and
*_archived_score variants).

Usage:
    poetry run python scripts/performance_report.py [--db-path data/forecasts.db]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import dotenv
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaculus_bot import db  # noqa: E402

logger = logging.getLogger(__name__)

_POSTS_URL = "https://www.metaculus.com/api/posts/{post_id}/"
_TIMEOUT_SECONDS = 15
_BINARY_QUESTION_TYPE = "BinaryQuestion"
_CALIBRATION_BINS = 10


def _latest_rows_per_question(conn: sqlite3.Connection, *, dry_run: bool | None) -> list[sqlite3.Row]:
    """One row per question_id -- whichever 'ok' forecast is most recent (a
    re-forecast supersedes an earlier one for analysis purposes).

    dry_run=False restricts to rows that were actually submitted to
    Metaculus (the only ones that can have an official score); dry_run=None
    considers both, for local-only metrics (Brier/calibration) that don't
    need an official submission, just our own predicted probability and the
    eventual outcome."""
    conn.row_factory = sqlite3.Row
    outer_clause = "" if dry_run is None else "AND f.dry_run = 0"
    inner_clause = "" if dry_run is None else "AND f2.dry_run = 0"
    rows = conn.execute(
        f"""
        SELECT f.* FROM forecasts f
        WHERE f.status = 'ok' AND f.question_id IS NOT NULL {outer_clause}
          AND f.run_timestamp = (
              SELECT MAX(f2.run_timestamp) FROM forecasts f2
              WHERE f2.question_id = f.question_id AND f2.status = 'ok' {inner_clause}
          )
        """
    ).fetchall()
    return rows


def fetch_question_json(post_id: int, token: str) -> dict[str, Any] | None:
    """GET-only. Returns None (and logs a warning) on any failure so one bad
    question can't take down the whole report."""
    try:
        response = requests.get(
            _POSTS_URL.format(post_id=post_id),
            headers={"Authorization": f"Token {token}", "Accept-Language": "en"},
            params={"with_cp": "true"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.warning(f"Could not fetch question data for post {post_id}: {e}")
        return None


def is_resolved(question_json: dict[str, Any]) -> bool:
    return question_json.get("question", {}).get("resolution") is not None


def parse_binary_outcome(question_json: dict[str, Any]) -> float | None:
    """1.0/0.0 for a resolved binary yes/no; None if unresolved, ambiguous,
    or annulled (those contribute to neither Brier nor calibration -- there's
    no well-defined "correct probability" for them)."""
    resolution = question_json.get("question", {}).get("resolution")
    if resolution == "yes":
        return 1.0
    if resolution == "no":
        return 0.0
    return None


def extract_score_data(question_json: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """(peer_score, baseline_score, coverage) from our own
    my_forecasts.score_data, preferring the tournament's spot-scoring variant
    when the non-spot one is absent (MiniBench and Market Pulse score with
    spot_peer/spot_baseline; the seasonal tournament uses the non-spot
    peer/baseline) -- either way there's one meaningful number per question."""
    score_data = question_json.get("question", {}).get("my_forecasts", {}).get("score_data") or {}
    peer = score_data.get("peer_score")
    if peer is None:
        peer = score_data.get("spot_peer_score")
    baseline = score_data.get("baseline_score")
    if baseline is None:
        baseline = score_data.get("spot_baseline_score")
    coverage = score_data.get("coverage")
    return peer, baseline, coverage


def community_prediction_at(question_json: dict[str, Any], at_timestamp: float) -> float | None:
    """The community aggregate's P(yes) at the time we made our forecast --
    the most recent CP history entry whose start_time is <= at_timestamp.
    Binary-only: a single `centers[0]` is only an unambiguous P(yes) for
    binary questions. Returns None if no aggregation history exists at or
    before that time."""
    aggregations = question_json.get("question", {}).get("aggregations") or {}
    best_entry: dict[str, Any] | None = None
    for agg in aggregations.values():
        history = agg.get("history") or []
        for entry in history:
            start_time = entry.get("start_time")
            if start_time is None or start_time > at_timestamp:
                continue
            if best_entry is None or start_time > best_entry.get("start_time", -1):
                best_entry = entry
        if best_entry is not None:
            break  # first aggregation method with any usable history is enough
    if best_entry is None:
        return None
    centers = best_entry.get("centers") or []
    return centers[0] if centers else None


def brier_score(predicted: float, outcome: float) -> float:
    return (predicted - outcome) ** 2


def calibration_table(pairs: list[tuple[float, float]], n_bins: int = _CALIBRATION_BINS) -> list[dict[str, Any]]:
    """pairs: list of (predicted_probability, outcome in {0.0, 1.0}). One row
    per bin [i/n_bins, (i+1)/n_bins) with count / avg predicted / empirical
    resolution rate; empty bins are omitted."""
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for p, o in pairs:
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, o))
    table = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        table.append(
            {
                "bin_low": i / n_bins,
                "bin_high": (i + 1) / n_bins,
                "count": len(bucket),
                "avg_predicted": sum(p for p, _ in bucket) / len(bucket),
                "empirical_rate": sum(o for _, o in bucket) / len(bucket),
            }
        )
    return table


def _safe_float(value: str | None) -> float | None:
    if value is None or value == "None":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_logged_value(value: str | None) -> float | None:
    """aggregate_value/post_calibration_value are stored double-encoded:
    bot.py's _record_debug already does str(value) before handing it to
    db.log_forecast, which then does json.dumps(..., default=str) on top --
    so the column literally contains '"0.42"' (with quotes) for a binary
    row. json.loads() once undoes db.log_forecast's layer and recovers the
    str()'d python value, which float() parses directly for binary.
    Non-binary types store a repr there instead (e.g. a PredictedOptionList's
    or NumericDistribution's) that legitimately isn't a float -- caught by
    the ValueError below and treated as not-applicable."""
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


def generate_report(conn: sqlite3.Connection, token: str | None) -> str:
    lines: list[str] = ["=== Performance report (resolved questions) ===\n"]

    if not token:
        lines.append("No METACULUS_TOKEN available -- skipping (can't look up official scores).")
        return "\n".join(lines)

    # ---- Official scores (peer/baseline), only for questions we actually
    # submitted to Metaculus -- dry-run forecasts were never scored.
    submitted_rows = _latest_rows_per_question(conn, dry_run=False)
    question_json_cache: dict[int, dict[str, Any] | None] = {}

    def _get(post_id: int) -> dict[str, Any] | None:
        if post_id not in question_json_cache:
            question_json_cache[post_id] = fetch_question_json(post_id, token)
        return question_json_cache[post_id]

    score_rows = []  # (tournament, question_type, peer, baseline, coverage)
    for row in submitted_rows:
        qjson = _get(row["post_id"])
        if qjson is None or not is_resolved(qjson):
            continue
        peer, baseline, coverage = extract_score_data(qjson)
        if peer is None and baseline is None:
            continue
        score_rows.append((row["tournament"], row["question_type"], peer, baseline, coverage))

    def _avg(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return sum(present) / len(present) if present else None

    if not score_rows:
        lines.append(
            "No resolved-and-scored questions yet (nothing has both resolved on "
            "Metaculus and had scores computed there)."
        )
    else:
        lines.append(f"Resolved + scored: {len(score_rows)}")
        overall_peer = _avg([r[2] for r in score_rows])
        overall_baseline = _avg([r[3] for r in score_rows])
        overall_coverage = _avg([r[4] for r in score_rows])
        lines.append(
            f"  Overall: avg peer={_fmt(overall_peer)}  avg baseline={_fmt(overall_baseline)}  "
            f"avg coverage={_fmt(overall_coverage)}"
        )

        lines.append("  By tournament:")
        for tournament in sorted({r[0] for r in score_rows if r[0] is not None}):
            subset = [r for r in score_rows if r[0] == tournament]
            lines.append(
                f"    {tournament:<12} n={len(subset):<3} avg peer={_fmt(_avg([r[2] for r in subset]))}  "
                f"avg baseline={_fmt(_avg([r[3] for r in subset]))}"
            )

        lines.append("  By question type:")
        for qtype in sorted({r[1] for r in score_rows if r[1] is not None}):
            subset = [r for r in score_rows if r[1] == qtype]
            lines.append(
                f"    {qtype:<22} n={len(subset):<3} avg peer={_fmt(_avg([r[2] for r in subset]))}  "
                f"avg baseline={_fmt(_avg([r[3] for r in subset]))}"
            )

    # ---- Local Brier/calibration analysis, binary questions only (the only
    # type with a single well-defined "correct probability"). Uses the
    # latest 'ok' forecast per question regardless of dry_run, since this
    # only needs our own predicted probability + the eventual outcome, not
    # an official Metaculus score.
    local_rows = [r for r in _latest_rows_per_question(conn, dry_run=None) if r["question_type"] == _BINARY_QUESTION_TYPE]

    per_model_pairs: dict[str, list[tuple[float, float]]] = {}
    ensemble_pairs: list[tuple[float, float]] = []
    calibrated_pairs: list[tuple[float, float]] = []
    cp_comparison: list[tuple[float, float, float]] = []  # (ours, community, outcome)

    for row in local_rows:
        qjson = _get(row["post_id"])
        if qjson is None:
            continue
        outcome = parse_binary_outcome(qjson)
        if outcome is None:
            continue

        aggregate_value = _parse_logged_value(row["aggregate_value"])
        post_calibration_value = _parse_logged_value(row["post_calibration_value"])
        if aggregate_value is not None:
            ensemble_pairs.append((aggregate_value, outcome))
        if post_calibration_value is not None:
            calibrated_pairs.append((post_calibration_value, outcome))

            forecast_ts = datetime.fromisoformat(row["run_timestamp"]).timestamp()
            community = community_prediction_at(qjson, forecast_ts)
            if community is not None:
                cp_comparison.append((post_calibration_value, community, outcome))

        try:
            raw_model_outputs = json.loads(row["raw_model_outputs"] or "[]")
        except json.JSONDecodeError:
            raw_model_outputs = []
        for entry in raw_model_outputs:
            if entry.get("error"):
                continue
            value = _safe_float(entry.get("parsed_value"))
            if value is None:
                continue
            per_model_pairs.setdefault(entry["model"], []).append((value, outcome))

    if not calibrated_pairs and not ensemble_pairs:
        lines.append("\nNo resolved binary questions yet for Brier/calibration analysis.")
        return "\n".join(lines)

    lines.append(f"\nBinary questions resolved (local analysis): {len(calibrated_pairs)}")

    if per_model_pairs:
        lines.append("  Per-model Brier (raw, pre-ensemble):")
        for model in sorted(per_model_pairs):
            pairs = per_model_pairs[model]
            brier = sum(brier_score(p, o) for p, o in pairs) / len(pairs)
            lines.append(f"    {model:<45} n={len(pairs):<3} Brier={brier:.4f}")

    if ensemble_pairs and calibrated_pairs:
        raw_brier = sum(brier_score(p, o) for p, o in ensemble_pairs) / len(ensemble_pairs)
        calibrated_brier = sum(brier_score(p, o) for p, o in calibrated_pairs) / len(calibrated_pairs)
        lines.append(
            f"  Raw ensemble Brier:      {raw_brier:.4f}  (n={len(ensemble_pairs)})\n"
            f"  Post-calibration Brier:  {calibrated_brier:.4f}  (n={len(calibrated_pairs)})"
        )

    if cp_comparison:
        avg_gap = sum(abs(ours - community) for ours, community, _ in cp_comparison) / len(cp_comparison)
        lines.append(
            f"  Avg |our forecast - community prediction at forecast time|: {avg_gap:.4f} (n={len(cp_comparison)})"
        )

    table = calibration_table(calibrated_pairs)
    if table:
        lines.append(f"  Calibration table ({_CALIBRATION_BINS} bins, post-calibration probability):")
        lines.append("    bin           n   avg predicted   empirical rate   gap")
        for bin_row in table:
            gap = bin_row["empirical_rate"] - bin_row["avg_predicted"]
            lines.append(
                f"    {bin_row['bin_low']:.1f}-{bin_row['bin_high']:.1f}     "
                f"{bin_row['count']:<3} {bin_row['avg_predicted']:<14.3f} {bin_row['empirical_rate']:<16.3f} {gap:+.3f}"
            )

    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


if __name__ == "__main__":
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Print a Metaculus performance report")
    parser.add_argument("--db-path", type=str, default="data/forecasts.db")
    args = parser.parse_args()

    with db.connect(args.db_path) as conn:
        print(generate_report(conn, os.getenv("METACULUS_TOKEN")))
