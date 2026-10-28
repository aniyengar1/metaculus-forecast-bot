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
        # Retry once on push rejection (e.g. another run pushed a newer
        # commit in between our fetch and our push -- the shared
        # concurrency group on the forecast workflows makes this unlikely
        # but not impossible at the exact boundary between a queued run
        # finishing and the next starting). Rebase onto the freshly
        # fetched tip before retrying -- if that itself conflicts (two
        # genuinely different versions of the binary db file), bail out
        # loudly rather than guessing how to resolve it.
        if ! git push origin data; then
            echo "git push to data branch failed -- fetching and rebasing before one retry."
            set +e
            git fetch origin data --quiet
            git rebase origin/data
            rebase_status=$?
            set -e
            if [ "$rebase_status" -ne 0 ]; then
                git rebase --abort >/dev/null 2>&1 || true
                echo "Rebase onto origin/data failed (likely a genuine conflict on the binary db file) -- giving up." >&2
                exit 1
            fi
            git push origin data
        fi
        echo "Pushed updated $DB_PATH to the data branch."
    fi
)
