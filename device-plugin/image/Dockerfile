# KRAKEN device-plugin/controller 재현 가능 이미지 (Exp_90 — exp77 실 레시피 복원).
#   ★ device-plugin/deploy/Dockerfile(distroless-static)은 실 빌드본이 아니었다(Exp_82 §4).
#   exp77 레이어 history 해부로 실제 구성 복원: ubuntu22.04 + python + /opt/kraken 레이아웃
#   + entrypoint 2종 + 232-빌드 .so. 빌드 입력 전부 git 추적(furiosa_native 115MB 벤더
#   바이너리는 plugin 광고(/dev/rngd discovery)에 불요라 생략 — Exp_90 A-3 검증).
#
#   빌드(컨텍스트=레포 루트):
#     docker build -f device-plugin/image/Dockerfile -t ketris-device-plugin:exp90 .
#     ctr -n k8s.io images import (또는 nerdctl로 k8s.io 네임스페이스에)
#
# ── 버전 고정 (재현성) ──
#   base ubuntu 22.04, go 1.26(go.mod), furiosa-sdk 0.10.2, numpy<2

# Stage 1: Go 바이너리 빌드 (host go=1.18 <  go.mod 1.26.0 → 컨테이너 빌더 필수)
FROM golang:1.26 AS build
ARG BUILD_STAMP=unstamped
WORKDIR /src
COPY device-plugin/go.mod device-plugin/go.sum ./
RUN go mod download
COPY device-plugin/ ./
RUN CGO_ENABLED=0 GOFLAGS=-trimpath \
    go build -ldflags "-X main.buildStamp=${BUILD_STAMP}" -o /kraken-device-plugin .

# Stage 2: 런타임 (exp77과 동일 레이아웃)
FROM ubuntu:22.04
ARG BUILD_STAMP=unstamped
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip curl netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir "furiosa-sdk==0.10.2" "numpy<2"

# /opt/kraken 레이아웃 (COPY 순서 = 변경 빈도 낮은 것부터)
COPY --from=build /kraken-device-plugin      /opt/kraken/kraken-device-plugin
COPY device-plugin/image/lib/               /opt/kraken/lib/
# [Exp_134] 빠른 종료용 sitecustomize — 정본은 bench/sitecustomize.py 하나다
#   (Exp_96 이 만든 파일. 이미지가 들고 있어야 라이브 주입이 가능하다).
COPY bench/sitecustomize.py                 /opt/kraken/py/sitecustomize.py
# [Exp_137] admission webhook — 호스트 프로세스가 아니라 파드로 상주한다.
#   device-plugin 이미지에 얹는다: python3 런타임이 이미 있고, 이미지가 하나면
#   빌드 스탬프·배포 검증 경로도 하나다(재현 자산을 늘리지 않는다 — Exp_88/90 교훈).
COPY webhook/webhook_server.py              /opt/kraken/webhook/webhook_server.py
COPY controller/                            /opt/kraken/controller/
# [Exp_126 1-A] 작업 특성 분석 계층. Exp_125 에서 **이 디렉터리가 이미지에 없어서**
#   Deep Scanner 6항목의 라이브 호출자가 0개였다.
#   ★전량 반입한다(604 KB, 이미지 670 MB 대비 0.09%) — 선별의 이득이 없고,
#     선별하면 "어느 모듈이 왜 빠졌는지"를 따로 추적해야 한다.
#   제외는 .dockerignore 가 담당: __pycache__/*.pyc(공통) + kernel_tracer.so.oldenv
#   (230 환경 빌드본 — 232 에서 무용하고 정본과 혼동된다).
COPY profiler/                              /opt/kraken/profiler/
COPY shm/                                   /opt/kraken/shm/
COPY runtime/npu_proxy/                     /opt/kraken/npu_proxy/
# [Exp_153 2부] 원격 풀 advertiser — CR 대기 루프 상주(사이드카).
#   Exp_152 까지 호스트 수동 기동이었다("CR만 올리면 붙는다" 불성립).
COPY remote_pool/                           /opt/kraken/remote_pool/
COPY lsu/catalog.json                       /opt/kraken/catalog.json
COPY device-plugin/image/entrypoint-plugin.sh device-plugin/image/entrypoint-controller.sh device-plugin/image/entrypoint-feedback.sh device-plugin/image/entrypoint-advertiser.sh /opt/kraken/
RUN chmod +x /opt/kraken/kraken-device-plugin /opt/kraken/entrypoint-*.sh
# [Exp_123 2부] 컨트롤러(파이썬)도 같은 문제를 갖는다 — 바이너리가 아니라 파일이라
#   ldflags 를 못 쓴다. 이미지에 스탬프 파일을 굽고 엔트리포인트가 찍는다.
RUN echo "${BUILD_STAMP}" > /opt/kraken/BUILD_STAMP

# [Exp_126 1-A] profiler 하위 모듈은 형제 패키지를 top-level 로 import 한다
#   (detector.py:23 `from kernel_class.patterns import ...`). tests/ 가 쓰는
#   sys.path 패턴과 같게 profiler 루트를 경로에 올린다.
ENV PYTHONPATH=/opt/kraken/shm:/opt/kraken/profiler
WORKDIR /opt/kraken
ENTRYPOINT ["/opt/kraken/entrypoint-plugin.sh"]
