#!/bin/bash
# [Exp_123 2부] 배포 검증용 빌드 스탬프.
#
# ★태그 비교로는 부족하다 — 두 함정이 서로 다른 실패였다:
#     Exp_121: 같은 태그로 내용만 바뀜 → 롤아웃이 안 돌아 구 코드가 계속 떴다 (태그는 맞다)
#     Exp_122: 매니페스트가 옛 태그를 못박음 → apply 마다 이미지가 되돌아갔다 (태그가 틀렸다)
#   스탬프는 **이미지에 들어간 소스 내용**에서 유도한다. 그러면 둘 다 잡힌다.
#
# 스탬프 = <git-sha[+dirty]>-<빌드 입력 내용 해시 12자>
#   - git sha: 사람이 읽고 커밋을 찾기 위한 것. 이것만으로는 부족하다(더티 트리 빌드가 흔하다)
#   - 내용 해시: 판정의 근거. **이미지에 COPY 되는 경로만** 넣는다(Dockerfile 과 같은 집합).
#     빌드 산출물·캐시는 제외한다 — 재현 빌드에서 스탬프가 흔들리면 검증이 무의미해진다.
set -u
cd "$(dirname "$0")/.."
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || SHA="${SHA}+dirty"
# ★해시 입력 = **이미지 내용을 결정하는 것만**. 두 가지를 명시적으로 뺀다:
#   - device-plugin/deploy/*.yaml : 배포 매니페스트다. 이미지에 안 들어간다.
#     넣으면 "태그를 바꾸려고 매니페스트를 고치면 스탬프가 바뀌어" 영원히 수렴하지 않는다
#     (실측: 태그 수정 → 스탬프 21c83945437f → 8adc15d0c20c 로 이동, 방금 빌드한 이미지가
#      즉시 불일치가 됐다).
#   - *.md / *.pyc / __pycache__ / *.tar : 문서·부산물
# [Exp_134] ★입력 집합이 Dockerfile 의 COPY 집합과 어긋나 있었다.
#   Exp_131 이 `COPY profiler/` 를, Exp_134 가 `COPY bench/sitecustomize.py` 를
#   추가했는데 여기에는 반영되지 않아, **이미지 내용이 바뀌어도 스탬프가 그대로**였다
#   (실측: sitecustomize 수정 전후 스탬프 동일). 검증 도구가 못 잡는 변경이 있으면
#   그 도구는 그만큼만 믿을 수 있다. COPY 집합과 맞춘다.
# [Exp_153] COPY remote_pool/ 추가에 맞춤 (advertiser 사이드카)
H=$(find device-plugin controller shm runtime/npu_proxy lsu/catalog.json \
      profiler bench/sitecustomize.py webhook/webhook_server.py remote_pool \
      -type f \
      ! -path 'device-plugin/deploy/*' \
      ! -name '*.md' ! -name '*.pyc' ! -path '*/__pycache__/*' ! -name '*.tar' \
      -print0 2>/dev/null | sort -z | xargs -0 sha256sum 2>/dev/null \
    | sha256sum | cut -c1-12)
echo "${SHA}-${H}"
