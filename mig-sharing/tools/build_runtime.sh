#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CUDA_TOOLKIT=${KETRIS_CUDA_HOME:-/usr/local/cuda-12.9}
BUILD_DIR=${KETRIS_BUILD_DIR:-"$TASK_ROOT/.build/runtime"}
mkdir -p "$BUILD_DIR"
g++ -std=c++17 -fsanitize=undefined -fno-sanitize-recover=all -I "$TASK_ROOT/runtime/libbless" "$TASK_ROOT/mig-sharing/tests/native/time_bootstrap_test.cpp" -o "$BUILD_DIR/time_bootstrap_test"
"$BUILD_DIR/time_bootstrap_test"
g++ -std=c++17 -pthread -fsanitize=undefined -fno-sanitize-recover=all -I "$TASK_ROOT/runtime/libbless" "$TASK_ROOT/mig-sharing/tests/native/time_refill_test.cpp" -o "$BUILD_DIR/time_refill_test"
"$BUILD_DIR/time_refill_test"
g++ -shared -fPIC -O2 -std=c++17 -I "$CUDA_TOOLKIT/include" "$TASK_ROOT/runtime/libbless/libbless.cpp" \
  -L "$CUDA_TOOLKIT/lib64" -lcuda -lcudart -ldl -lpthread -o "$BUILD_DIR/libbless.so"
"$CUDA_TOOLKIT/bin/nvcc" --cudart shared -O2 -arch=sm_120 "$TASK_ROOT/mig-sharing/tests/native/runtime_start_probe.cu" -o "$BUILD_DIR/runtime_start_probe"
sha256sum "$BUILD_DIR/libbless.so" "$BUILD_DIR/runtime_start_probe"
