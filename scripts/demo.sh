#!/bin/sh
# Start the demo from a clean state and open the page.
#   scripts/demo.sh          real cluster (kind cluster "stopline")
#   scripts/demo.sh mock     in-process cluster, no Docker needed
# Extra arguments are passed on, for example: scripts/demo.sh kind --voice --model
set -eu
cd "$(dirname "$0")/.."
cluster="${1:-kind}"
[ $# -gt 0 ] && shift
page=8785
router=8766

for port in "$page" "$router"; do
    for pid in $(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
        kill "$pid" 2>/dev/null || true
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
trap 'kill "$server" 2>/dev/null || true' INT TERM
for _ in $(seq 1 40); do
    if curl -s -o /dev/null "http://127.0.0.1:$page/"; then
        echo "StopLine is up: http://127.0.0.1:$page  (Ctrl+C here to stop it)"
        command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$page"
        break
    fi
    sleep 0.5
done
wait "$server"
