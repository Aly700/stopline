#!/bin/bash
# Run each test directory in its own pytest process (their helper modules share names).
cd "$(dirname "$0")/.." || exit 1
status=0
for dir in tests/*/; do
    printf '%s ' "$dir"
    output=$(uv run pytest "$dir" -o addopts="" -q "$@" 2>&1) || status=1
    printf '%s\n' "$output" | tail -1
done
exit $status
