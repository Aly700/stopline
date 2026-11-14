#!/bin/sh
# Run each test directory in its own pytest process (their helper modules share names).
cd "$(dirname "$0")/.." || exit 1
status=0
for dir in tests/*/; do
    printf '%s ' "$dir"
    uv run pytest "$dir" -o addopts="" -q "$@" 2>&1 | tail -1
    [ "${PIPESTATUS:-0}" = 0 ] || status=1
done
exit $status
