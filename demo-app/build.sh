#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Fail before building if the base is not local. No pulls or package installs.
docker image inspect alpine:3.17 >/dev/null
for version in v1 v2; do
    docker build --pull=false --network=none --build-arg "VERSION=$version" \
        -t "stopline/checkout:$version" "$app_dir"
    kind load docker-image "stopline/checkout:$version" --name stopline
done
