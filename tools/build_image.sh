#!/bin/bash
# [Exp_161 1-C] 이미지 빌드 래퍼 — 시험 스택 재현성의 거부 장치.
#   문서 규칙("클린 트리에서 빌드")이 두 번 흘렀다(Exp_140 규칙 → Exp_153부터 dirty).
#   도구가 막는다: 작업트리가 dirty 면 빌드 거부. 실험 빌드는 ALLOW_DIRTY=1 로만 열고,
#   그 스탬프(+dirty)는 시험 스택이 될 수 없다 — deploy_verify ⑨ 가 경고한다.
# 사용: tools/build_image.sh <태그>   (예: exp162)
set -eu
cd "$(dirname "$0")/.."
TAG=${1:?태그 필요 (예: exp162)}
if ! git diff --quiet || ! git diff --cached --quiet; then
  if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
    echo "❌ 작업트리가 dirty — 시험 스택 재현성 위반. 커밋 후 빌드하거나,"
    echo "   실험 빌드면 ALLOW_DIRTY=1 (그 이미지는 시험 스택 불가 — 스탬프에 +dirty)."
    exit 1
  fi
  echo "⚠ ALLOW_DIRTY=1 — 이 이미지는 시험 스택이 될 수 없다(+dirty 스탬프)"
fi
STAMP=$(tools/build_stamp.sh)
echo "BUILD_STAMP=$STAMP"
docker build -f device-plugin/image/Dockerfile --build-arg BUILD_STAMP="$STAMP" \
  -t "ketris-device-plugin:$TAG" .
docker save "ketris-device-plugin:$TAG" | ctr -n k8s.io images import -
echo "✅ ketris-device-plugin:$TAG ($STAMP)"
