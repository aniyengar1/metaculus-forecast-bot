#!/usr/bin/env bash
# Commits the current data/forecasts.db to the orphan `data` branch and
# pushes it, creating that branch if it doesn't exist yet. Uses a throwaway
# git worktree so this never touches the calling workflow's actual checkout
# (main branch working tree, staged changes, etc). See restore_data_branch.sh
# for why a data branch rather than an artifact.
set -euo pipefail

DB_PATH="${1:-data/forecasts.db}"

if [ ! -f "$DB_PATH" ]; then
    echo "No $DB_PATH to persist, skipping."
    exit 0
fi

WORKTREE_DIR="$(mktemp -d)"
trap 'git worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true' EXIT

if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
    git fetch origin data --quiet
    git worktree add --quiet "$WORKTREE_DIR" data
else
    git worktree add --quiet --detach "$WORKTREE_DIR"
    (cd "$WORKTREE_DIR" && git checkout --orphan data && git rm -rf . >/dev/null 2>&1 || true)
fi

mkdir -p "$WORKTREE_DIR/$(dirname "$DB_PATH")"
cp "$DB_PATH" "$WORKTREE_DIR/$DB_PATH"

(
    cd "$WORKTREE_DIR"
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add "$DB_PATH"
    if git diff --cached --quiet; then
        echo "No changes to $DB_PATH, nothing to commit."
    else
        git commit --quiet -m "Update forecast DB [skip ci]"
        git push origin data
        echo "Pushed updated $DB_PATH to the data branch."
    fi
)
