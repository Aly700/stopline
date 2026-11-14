#!/bin/sh
# Start the demo from a clean state.
#   scripts/demo.sh          real cluster (kind cluster "stopline")
#   scripts/demo.sh mock     in-process cluster, no Docker needed
# Extra arguments are passed on, for example: scripts/demo.sh kind --voice --model
# Keep ONE browser tab on the page: each open tab holds a live connection.
set -eu
cd "$(dirname "$0")/.."
cluster="${1:-kind}"
[ $# -gt 0 ] && shift
page=8785
router=8766

# Stop any earlier instance completely: the server, its router and its workers.
pkill -9 -f "stopline --cluster" 2>/dev/null || true
pkill -9 -f "stopline.targets.router --port $router" 2>/dev/null || true
pkill -9 -f "stopline.agents.worker" 2>/dev/null || true
for port in "$page" "$router"; do
    for pid in $(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
        kill -9 "$pid" 2>/dev/null || true
    done
done
sleep 1

if [ "$cluster" = kind ]; then
    if ! kubectl --context kind-stopline -n checkout get deployment checkout-api >/dev/null 2>&1; then
        echo "setting up the cluster target"
        demo-app/build.sh
        k8s/up.sh
    fi
fi

uv run python -m stopline --cluster "$cluster" --fresh "$@" &
server=$!
trap 'kill "$server" 2>/dev/null || true; pkill -9 -f "stopline --cluster" 2>/dev/null || true; pkill -9 -f "stopline.targets.router --port $router" 2>/dev/null || true' INT TERM EXIT
for _ in $(seq 1 60); do
    if curl -s -o /dev/null "http://127.0.0.1:$page/"; then
        echo "StopLine is up: http://127.0.0.1:$page  (Ctrl+C here to stop it; keep one tab open)"
        break
    fi
    sleep 0.5
done
wait "$server"
