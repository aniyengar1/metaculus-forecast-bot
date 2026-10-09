#!/usr/bin/env bash
# Restores data/forecasts.db from the orphan `data` branch into the current
# working tree, if that branch and file exist. No-op (just ensures the
# parent directory exists) on a fresh repo with no data branch yet.
#
# Why a data branch and not a GitHub Actions artifact: artifacts are scoped
# per-workflow-run and need an extra action (or API lookup) to find "the
# latest successful run's artifact" across runs/workflows; they also expire
# (default 90 days) and aren't browsable. A plain git branch holding just
# this one file needs nothing but git itself, survives indefinitely, and
# since this repo is public anyway, the forecast history being inspectable
# via a normal `git clone` is a feature, not a cost. The tradeoff is a
# slowly-growing binary blob in git history, which at a few KB/run is a
# non-issue at this scale.
set -euo pipefail

DB_PATH="${1:-data/forecasts.db}"
mkdir -p "$(dirname "$DB_PATH")"

if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
    git fetch origin data --quiet
    if git show "origin/data:$DB_PATH" > "$DB_PATH" 2>/dev/null; then
        echo "Restored $DB_PATH from the data branch ($(du -h "$DB_PATH" | cut -f1))."
    else
        echo "data branch exists but has no $DB_PATH yet; starting fresh."
    fi
else
    echo "No data branch on origin yet; starting fresh."
fi
