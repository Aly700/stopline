#!/bin/sh
set -eu
k8s_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

kubectl --context kind-stopline -n checkout --request-timeout=3s \
    apply -f "$k8s_dir/namespace.yaml" -f "$k8s_dir/deployments.yaml"
kubectl --context kind-stopline -n checkout --request-timeout=5s \
    rollout status deployment/checkout-api --timeout=20s
kubectl --context kind-stopline -n checkout --request-timeout=5s \
    rollout status deployment/checkout-worker --timeout=20s
