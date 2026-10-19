"""
One-time backfill: migrates each model-run entry's `parsed_value` in
existing `forecasts` rows from the pre-2026-10-18 format (a Python
str(value) repr, e.g. "0.62" or "PredictedOptionList(predicted_options=
[...])") to the new clean, directly JSON-usable format (see
metaculus_bot.bot._structured_parsed_value).

Deliberately does NOT re-parse raw_text (which can be lossy -- truncated
at 4000 chars pre-fix, confirmed to cut off a real model's answer block
entirely). Instead extracts from the EXISTING parsed_value repr string,
which was always built from the full in-memory value, before any
truncation -- so it recovers essentially everything raw_text re-parsing
would miss:

- Binary: the repr is already just a plain float string -- exact,
  lossless conversion, no regex needed.
- Multiple choice: regex-extracts option_name/probability pairs from the
  PredictedOptionList repr -- exact, lossless.
- Numeric/discrete/date: regex-extracts Percentile(percentile=..,
  value=..) pairs from the NumericDistribution repr. This is the
  ALREADY-BUILT ~201-point PCHIP-expanded distribution, not the model's
  original sparse elicited points (which the new code stores directly
  going forward, but isn't recoverable for historical rows without
  raw_text). Backfilled rows therefore store 3 REPRESENTATIVE percentiles
  (closest match to 10/50/90) from that expanded distribution --
  sufficient for median/P10/P90-based analysis, but documented here as a
  different (coarser) provenance than new rows.

Usage:
    poetry run python scripts/backfill_parsed_values.py [--db-path data/forecasts.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaculus_bot import db  # noqa: E402

_OPTION_RE = re.compile(r"option_name='([^']*)', probability=([\d.eE+-]+)")
_PERCENTILE_RE = re.compile(r"Percentile\(percentile=([\d.eE+-]+), value=([\d.eE+-]+)\)")
_REPRESENTATIVE_TARGETS = (0.1, 0.5, 0.9)


def backfill_entry(entry: dict[str, Any], question_type: str) -> tuple[Any, str | None]:
    """Returns (new_parsed_value, skip_reason); skip_reason is None on
    success (including the no-op case: already-None-on-error, or already
    migrated to the new format)."""
    if entry.get("error"):
        return None, None  # correctly None already -- no value was ever produced

    old = entry.get("parsed_value")
    if not isinstance(old, str):
        return old, None  # already the new format (float/dict/list) -- no-op

    if old in ("None", ""):
        return None, None  # nothing to recover

    if question_type == "BinaryQuestion":
        try:
            return float(old), None
        except ValueError:
            return None, f"could not parse a float from {old[:60]!r}"

    if question_type == "MultipleChoiceQuestion":
        pairs = _OPTION_RE.findall(old)
        if not pairs:
            return None, "no option_name/probability pairs found in the stored repr"
        return {name: float(prob) for name, prob in pairs}, None

    if question_type in ("NumericQuestion", "DiscreteQuestion", "DateQuestion"):
        pairs = _PERCENTILE_RE.findall(old)
        if not pairs:
            return None, "no Percentile(...) pairs found in the stored repr"
        points = [(float(p), float(v)) for p, v in pairs]
        representative = [
            {"percentile": closest[0], "value": closest[1]}
            for closest in (min(points, key=lambda pv: abs(pv[0] - target)) for target in _REPRESENTATIVE_TARGETS)
        ]
        return representative, None

    return None, f"unknown question_type {question_type!r}"


def backfill_all(conn: sqlite3.Connection, *, apply: bool) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, post_id, question_type, raw_model_outputs FROM forecasts "
        "WHERE raw_model_outputs IS NOT NULL AND raw_model_outputs != '[]'"
    ).fetchall()

    migrated_entries = 0
    already_clean_entries = 0
    error_entries = 0  # correctly parsed_value=None already -- nothing to migrate
    failures: list[dict[str, Any]] = []
    rows_changed = 0

    for row_id, post_id, question_type, raw in rows:
        try:
            outputs = json.loads(raw)
        except json.JSONDecodeError:
            failures.append({"row_id": row_id, "post_id": post_id, "model": None, "reason": "raw_model_outputs is not valid JSON"})
            continue

        row_changed = False
        for entry in outputs:
            old = entry.get("parsed_value")
            new_value, reason = backfill_entry(entry, question_type)
            if reason is not None:
                failures.append({"row_id": row_id, "post_id": post_id, "model": entry.get("model"), "reason": reason})
                continue
            if entry.get("error"):
                error_entries += 1
            elif isinstance(old, str):
                entry["parsed_value"] = new_value
                migrated_entries += 1
                row_changed = True
            else:
                already_clean_entries += 1

        if row_changed:
            rows_changed += 1
            if apply:
                conn.execute(
                    "UPDATE forecasts SET raw_model_outputs = ? WHERE id = ?",
                    (json.dumps(outputs), row_id),
                )

    if apply:
        conn.commit()

    return {
        "rows_scanned": len(rows),
        "rows_changed": rows_changed,
        "migrated_entries": migrated_entries,
        "already_clean_entries": already_clean_entries,
        "error_entries": error_entries,
        "failures": failures,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill clean parsed_value for existing forecast rows")
    parser.add_argument("--db-path", type=str, default="data/forecasts.db")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    args = parser.parse_args()

    with db.connect(args.db_path) as conn:
        result = backfill_all(conn, apply=not args.dry_run)

    print(f"Rows scanned: {result['rows_scanned']}")
    print(f"Rows changed: {result['rows_changed']}" + (" (dry run -- not written)" if args.dry_run else ""))
    print(f"Entries migrated: {result['migrated_entries']}")
    print(f"Entries already clean: {result['already_clean_entries']}")
    print(f"Entries correctly None (model run errored, nothing to migrate): {result['error_entries']}")
    print(f"Entries that could not be backfilled: {len(result['failures'])}")
    for f in result["failures"]:
        print(f"  row={f['row_id']} post={f['post_id']} model={f['model']!r}: {f['reason']}")
