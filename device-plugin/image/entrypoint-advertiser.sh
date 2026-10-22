#!/bin/bash
# [Exp_153 2부] advertiser 사이드카 엔트리포인트.
#   KRAKEN_ADVERTISER=1(기본) → CR 대기 루프 상주(pool_advertiser.py, in-cluster API).
#   =0 → 기능 꺼짐(sleep 유지 — 컨테이너는 두되 아무 것도 안 한다).
set -u
if [ "${KRAKEN_ADVERTISER:-1}" != "1" ]; then
  echo "[advertiser] KRAKEN_ADVERTISER=${KRAKEN_ADVERTISER} → 기능 꺼짐(대기만)"
  exec sleep infinity
fi
echo "[advertiser] CR 대기 루프 상주 시작 (in-cluster API)"
exec python3 /opt/kraken/remote_pool/pool_advertiser.py
