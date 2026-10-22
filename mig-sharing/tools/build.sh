#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$TASK_ROOT"
TAG=${1:-mig-20261013-v9}
CONTEXT="$TASK_ROOT/.build/mig-image"
mkdir -p "$CONTEXT/package"
bash mig-sharing/tools/build_runtime.sh
CGO_ENABLED=0 /usr/local/go/bin/go -C mig-sharing/device_plugin build -trimpath -o "$CONTEXT/ketris-mig-plugin" .
STAMP=$(git rev-parse --short HEAD)
CGO_ENABLED=0 /usr/local/go/bin/go -C device-plugin build -trimpath -ldflags "-X main.buildStamp=$STAMP" -o "$CONTEXT/kraken-device-plugin" .
cp .build/runtime/libbless.so .build/runtime/runtime_start_probe "$CONTEXT/"
cp -r mig-sharing/ketris_mig "$CONTEXT/package/"
cp mig-sharing/pyproject.toml "$CONTEXT/package/"
cp webhook/webhook_server.py "$CONTEXT/"
printf '%s\n' "$STAMP" > "$CONTEXT/BUILD_STAMP"
docker build -f mig-sharing/Dockerfile -t "ketris-device-plugin:$TAG" "$CONTEXT"
docker save "ketris-device-plugin:$TAG" | ctr -n k8s.io images import -
docker image inspect "ketris-device-plugin:$TAG" --format '{{.Id}}' > "$CONTEXT/image-id.txt"
sha256sum "$CONTEXT/ketris-mig-plugin" "$CONTEXT/libbless.so" "$CONTEXT/kraken-device-plugin" > "$CONTEXT/artifact-sha256.txt"
