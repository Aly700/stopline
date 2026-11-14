#!/bin/sh
set -eu
k8s_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Foreground deletion removes the old ReplicaSets too, so an old action id
# cannot be mistaken for an action in the next demonstration.
kubectl --context kind-stopline -n checkout --request-timeout=5s \
    delete deployment checkout-api checkout-worker --ignore-not-found \
    --cascade=foreground --wait=true --timeout=5s
exec "$k8s_dir/up.sh"
