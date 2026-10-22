#!/bin/bash
# [Exp_107 T-4/T-4-1] 공용 페어 러너 — 개명 반영 + charged 스냅샷 시점 교정.
#
# ★T-4-1: Exp_106 에서 내부 charged 와 실제 처리량 대조가 무산됐다. 측정 종료 후
#   파드가 사라져 /stats 를 못 읽었기 때문이다. 여기서는 **완료 대기 중 주기적으로**
#   /stats·/feeder/status 를 찍어 파드 생존 구간의 회계를 남긴다(U-2 대비).
#
# usage: run_pair.sh <tag> <lsuA> <lsuB> <classA> <classB> <dur> [batch]
#   env ROLE_A / ROLE_B — 워커 역할(기본 bert_infer). [Exp_110 M-5] prefill/decode 조합용.
#   env EXTRA_ENV — 파드 env 에 끼워 넣을 YAML 줄(들). [Exp_114 2-D] 공간 모드 주입용.
#     예: EXTRA_ENV='        - {name: BLESS_SPACE_MODE, value: "qmd"}'
#     ★sed 치환이라 개행이 들어가면 깨진다 — 한 줄씩만.
#   결과: $OUT(기본 /root/kraken_pair_<tag>.txt), 스냅샷: $SYNC/stats_<tag>_<n>.json
set -u
REPO=/root/workspace/pooling/prism
T=$REPO/bench/harness/pair.tmpl
SYNC=${SYNC:-/root/kraken-sync}
CTRL=${CTRL:-http://127.0.0.1:8090}
TAG=$1; LA=$2; LB=$3; CA=$4; CB=$5; DUR=$6; B=${7:-16}
OUT=${OUT:-/root/kraken_pair_$TAG.txt}

# [Exp_121 2-A] 한쪽만 쉬게 하려면 파드별 env 가 필요하다(EXTRA_ENV 는 양쪽 공통).
#   EXTRA_ENV_A/EXTRA_ENV_B 가 있으면 그쪽이 우선, 없으면 EXTRA_ENV 로 떨어진다.
mk(){ local xe="${6:-${EXTRA_ENV:-}}"
      sed "s|@@POD@@|$1|;s|@@CLASS@@|$2|;s|@@WN@@|$3|;s|@@B@@|$B|;s|@@LSU@@|$4|;s|@@DUR@@|$DUR|;s|@@REPEATS@@|1|;s|@@OBS@@|${OBS:-0}|;s|@@ROLE@@|$5|;s|@@EXTRA_ENV@@|$xe|" \
      $T | grep -v '^#' | kubectl apply -f - >/dev/null 2>>/tmp/pair_apply_err.log \
        || echo "[경고] 파드 apply 실패 ($1) — /tmp/pair_apply_err.log 확인"; }

# [Exp_112 T-5] apply stderr 수집 — Exp_93 §7-3 대문자 테넌트명 RFC1123 위반이
#   조용히 전량 거부돼 측정 실패로 오인된 전례가 있다.
: > /tmp/pair_apply_err.log 2>/dev/null
kubectl delete pod --all -n default --force --grace-period=0 >/dev/null 2>&1; sleep 4
rm -f $SYNC/result_*.json $SYNC/ready_* $SYNC/go $SYNC/stats_${TAG}_*.json 2>/dev/null
: > $OUT
mk p-a "$CA" pa "$LA" "${ROLE_A:-bert_infer}" "${EXTRA_ENV_A:-${EXTRA_ENV:-}}"
mk p-b "$CB" pb "$LB" "${ROLE_B:-bert_infer}" "${EXTRA_ENV_B:-${EXTRA_ENV:-}}"

# feeder 자동 등록 대기(6s 폴링)
for i in $(seq 1 40); do
  n=$(curl -s --max-time 5 $CTRL/feeder/status 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('tenants',{})))" 2>/dev/null)
  [ "${n:-0}" -ge 2 ] && break; sleep 3
done
echo "== tag=$TAG lsu=$LA:$LB class=$CA:$CB dur=$DUR batch=$B" >> $OUT
echo -n "   tenants: " >> $OUT
curl -s --max-time 5 $CTRL/feeder/status >> $OUT 2>/dev/null; echo >> $OUT

# ★워커 프로토콜: ready_<name> → go → 측정 → result_<name>.json
for i in $(seq 1 40); do [ "$(ls $SYNC/ready_p* 2>/dev/null | wc -l)" -ge 2 ] && break; sleep 2; done
touch $SYNC/go

# ★T-4-1 파드 생존 중 회계 스냅샷 (2초 간격)
snap=0
for i in $(seq 1 $((DUR/2 + 20))); do
  c=$(ls $SYNC/result_p*.json 2>/dev/null | wc -l); [ "$c" -ge 2 ] && break
  curl -s --max-time 3 $CTRL/stats > $SYNC/stats_${TAG}_${snap}.json 2>/dev/null
  snap=$((snap+1)); sleep 2
done
echo "   stats_snapshots: $snap" >> $OUT
# 마지막(가장 늦은) 스냅샷을 본문에 남긴다 — 파드 생존 구간의 최종 회계
last=$((snap-1)); [ $last -ge 0 ] && { echo -n "   stats_last: " >> $OUT; cat $SYNC/stats_${TAG}_${last}.json >> $OUT 2>/dev/null; echo >> $OUT; }
for f in $SYNC/result_pa.json $SYNC/result_pb.json; do
  [ -f "$f" ] && echo "   $(basename $f): $(cat $f)" >> $OUT
done
# [Exp_116 3부] feed.log 스냅샷 — kernels/total 은 파드가 살아 있을 때만 읽을 수 있다.
for d in /var/lib/kraken/socks/*/; do
  ls $d*.sock >/dev/null 2>&1 || continue
  age=$(( $(date +%s) - $(stat -c %Y $d 2>/dev/null) ))
  [ $age -lt 300 ] && echo "   feed[$(basename $d)]: $(grep -o 'total=[0-9]* kernels=[0-9]*' $d/feed.log 2>/dev/null | tail -1)" >> $OUT
done
kubectl delete pod --all -n default --force --grace-period=0 >/dev/null 2>&1
