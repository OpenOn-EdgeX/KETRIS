#!/bin/bash
# [Exp_151 4-A] ④ 간섭 제어 상주 엔트리포인트 — controller 파드의 3번째 사이드카.
#
# 형태 근거(Exp_137 기준): 호스트 nohup 은 재부팅·재시작에 유실된다 — webhook 을
# Deployment 로 옮긴 것과 같은 이유로 파드에 넣는다. closed_loop 은 controller/
# 아래라 이미지 스탬프에 이미 들어가므로(Exp_145 확인) 사이드카 결합이 추가 비용을
# 만들지 않고, /root/kraken-sync(hostPath)·localhost:8090(hostNetwork)을 공유한다.
#
# 스위치(4-I): KRAKEN_FEEDBACK — 코드 기본과 DS 를 일치시킨다.
#   "0"(기본) = 관측 전용(--observe-only): 판정·/healthz·retention 보고는 하되
#               actuate 를 하지 않는다. 6부 판정 — G1(Exp_89 재현)이 현 배율에서
#               성립하지 않아 개입 활성의 근거가 없다. G8·G10(신호 노출)만 산다.
#   "1"       = 개입 활성(k=1.7 코드 기본, Exp_89 유래).
set -u
MODE=${KRAKEN_FEEDBACK:-0}
EXTRA=""
if [ "$MODE" != "1" ]; then
  EXTRA="--observe-only"
  echo "[feedback] KRAKEN_FEEDBACK=$MODE → 관측 전용(개입 없음, 신호·유지율 노출만)"
else
  echo "[feedback] KRAKEN_FEEDBACK=1 → 개입 활성(k-intervene 코드 기본 1.7)"
fi
exec python3 /opt/kraken/controller/interference/closed_loop.py \
  --sync-dir /root/kraken-sync \
  --feeder-url http://127.0.0.1:8090 \
  --interval 0.5 \
  --health-port 18081 \
  --report-retention \
  --emit-events \
  --log /var/lib/kraken/closed_loop.log \
  --tag resident $EXTRA
