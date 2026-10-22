#!/bin/bash
# KRAKEN device-plugin 컨테이너 엔트리포인트 (Exp_42)
#
# Allocate 는 --bless-so / --bless-sock-dir 경로를 "호스트 경로 그대로" 워크로드
# 파드에 hostPath 마운트한다 (plugin.go). 따라서 이미지에 담긴 .so 를 기동 시
# hostPath 볼륨(/var/lib/kraken)으로 복사해 호스트에 실존시킨다 — 노드 상태가
# 이미지에서 파생되는 구조 (호스트 수동 배치 불필요).
set -e

mkdir -p /var/lib/kraken/lib /var/lib/kraken/socks

# [Exp_140 1-A] 원자 교체 — cp -f(O_TRUNC 제자리 덮어쓰기)는 이 .so 를 mmap 한
#   실행 중 LSU 파드를 죽인다(Exp_138 §11 사고, Exp_139 5-D에서 20/20 사망 —
#   내용이 같아도 죽는다). rename(2)은 원자적이고 기존 매핑은 옛 inode 를 계속
#   잡으므로 무영향(Exp_139 5-D: install+mv 20/20 생존).
#   임시 파일은 대상과 같은 디렉터리(같은 파일시스템)에 만들어야 mv 가
#   rename(2)으로 떨어진다. 실패 시 임시 파일을 남기지 않는다.
place(){ install -m 0755 "$1" "$2.tmp.$$" && mv -f "$2.tmp.$$" "$2" || { rm -f "$2.tmp.$$"; return 1; }; }
place /opt/kraken/lib/libbless.so   /var/lib/kraken/lib/libbless.so
place /opt/kraken/lib/gating_lib.so /var/lib/kraken/lib/gating_lib.so
# [Exp_131] CUPTI 커널 트레이서 — libbless 와 같은 hostPath 경로로 내린다.
#   플러그인이 --tracer-so 로 이 파일을 가리킨다(기본 꺼짐이라 없어도 무해하지만,
#   이미지가 들고 있어야 노드 재구축 후에도 켤 수 있다).
place /opt/kraken/lib/kernel_tracer.so /var/lib/kraken/lib/kernel_tracer.so
# [Exp_134] 빠른 종료용 sitecustomize 를 hostPath 로 — Allocate 가 이 디렉터리를
#   PYTHONPATH 로 워크로드에 마운트한다(--fast-teardown-dir). .so 들과 같은 규약.
#   (.py 도 place — import 중 truncate 창을 밟으면 워크로드 기동이 깨진다)
mkdir -p /var/lib/kraken/py
place /opt/kraken/py/sitecustomize.py /var/lib/kraken/py/sitecustomize.py
echo "[entrypoint-plugin] hostPath 배치 완료: $(md5sum /var/lib/kraken/lib/*.so | awk '{print $1" "$2}' | tr '\n' ' ')"

# controller 사이드카 대기 (기동 순서 비결정 대비, 최대 60s)
for i in $(seq 1 60); do
  curl -s --max-time 1 http://127.0.0.1:8090/status > /dev/null && break
  sleep 1
done
curl -s --max-time 1 http://127.0.0.1:8090/status > /dev/null \
  && echo "[entrypoint-plugin] controller OK" \
  || echo "[entrypoint-plugin] 경고: controller 8090 무응답 — plugin 은 계속 기동"

# [Exp_134 3-B] 종료 감지 이벤트 생산자 — KRAKEN_BOOSTER=1 일 때만.
#   term_online.py 는 nvidia-smi 를 100ms 폴링한다. controller 컨테이너에는
#   nvidia-smi 도 /dev 도 없으므로 **여기(plugin)** 가 유일하게 띄울 수 있는 자리다.
#   이벤트 파일은 /var/lib/kraken(양 컨테이너 공유 hostPath)에 두고 controller 의
#   Booster 가 구독한다.
#   ⚠️ 샘플 로그는 100ms 마다 한 줄씩 쌓인다(하루 약 86만 줄). 상시 켜기 전에
#      회전 정책이 필요하다 — 기본 꺼짐인 이유 중 하나.
if [ "${KRAKEN_BOOSTER:-0}" = "1" ]; then
  UUID="$(nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -1)"
  if [ -n "$UUID" ]; then
    rm -f /var/lib/kraken/term_stop
    : > /var/lib/kraken/term_samples.jsonl
    python3 /opt/kraken/profiler/timeseries/term_online.py "$UUID" \
      /var/lib/kraken/term_samples.jsonl /var/lib/kraken/term_stop \
      --events /var/lib/kraken/term_events.jsonl \
      >> /var/lib/kraken/term_online.log 2>&1 &
    echo "[entrypoint-plugin] 종료 감지 생산자 기동: gpu=$UUID pid=$!"
  else
    # [T-5] 조용히 넘어가지 않는다 — 생산자가 없으면 Booster 는 영영 actions=0 이다
    echo "[entrypoint-plugin][경고] GPU UUID 조회 실패 — 종료 감지 생산자를 띄우지 못했다."
  fi
fi

exec /opt/kraken/kraken-device-plugin "$@"
