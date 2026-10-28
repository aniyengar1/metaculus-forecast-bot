#!/usr/bin/env bash
# Retries `poetry install` up to 3 times with a 10s delay between attempts.
# A transient network blip mid-download (confirmed 2026-10-20: a real CI
# failure was a ChunkedEncodingError/IncompleteRead partway through
# fetching `jedi`'s wheel -- nothing to do with our code, Metaculus, or
# any LLM provider) shouldn't fail the whole run and trigger a failure
# email if it would have succeeded seconds later on retry. Still fails
# the step (and the run) if all 3 attempts fail -- a persistently broken
# install is a real problem, not a blip.
set -uo pipefail

for attempt in 1 2 3; do
    if poetry install --no-interaction --no-root "$@"; then
        exit 0
    fi
    echo "poetry install failed (attempt $attempt/3)"
    if [ "$attempt" -lt 3 ]; then
        sleep 10
    fi
done

echo "poetry install failed after 3 attempts -- not retrying further."
exit 1
