#!/bin/bash
# 발표용 통합 데모 (Exp_82 Part D). 원샷 실행, ~1분. 산=간섭 진입+제어 전환 순간.
# 전제: factor 1.5(광고 267), controller /capacity(:8090)·exporter(:9095, KRAKEN_RESULT_DIR) 가동, kraken-demo NS, webhook 가동.
set -u
ROOT=/root/workspace/pooling/prism
DEMO=$ROOT/integration_demo
NS=kraken-demo; SYNC=/root/kraken-demo-sync
export KRAKEN_CAP_DIR=/var/lib/kraken/capability
ev(){ python3 $DEMO/orchestro_stub/emit_event.py "$@" >/dev/null 2>&1; }
cap(){ curl -s http://localhost:8090/capacity | python3 -c "import json,sys;d=json.load(sys.stdin)['devices'][0];c=d['capacity'];h=d['interference_hint'];z='간섭' if c['allocation_ratio']>=1.4 else('전이' if c['allocation_ratio']>=1.0 else '무간섭');print('   총할당률=%.2f (%s) armed=%s'%(c['allocation_ratio'],z,[ (s['tenant'][-10:],s['armed']) for s in d['slices']]))"; }
sub(){ # <name> <class> <stage> <batch>  — annotation만(webhook이 env 변환)
  sed "s|@@NAME@@|$1|;s|@@NS@@|$NS|;s|@@CLASS@@|$2|;s|@@BATCH@@|$4|;s|@@LSU@@|133|;s|@@STAGE@@|$3|" \
    $DEMO/korea_stub/workload_pod.tmpl.yaml | grep -v 'KRAKEN_WORKLOAD_CLASS' | kubectl apply -f - >/dev/null; }
wait_run(){ for i in $(seq 1 60); do kubectl get pod $1 -n $NS --no-headers 2>/dev/null|grep -q Running && return; sleep 2; done; }
step(){ echo ""; echo "━━━ [$(date +%H:%M:%S)] $1"; }
T0=$(date +%s); rm -f $SYNC/* 2>/dev/null; mkdir -p $SYNC

step "1) 하스퍼 capability 발행 → KETI 카탈로그 등재"
python3 $DEMO/hasper_stub/hasper_capability_agent.py | sed 's/^/   /'
ev SliceCreated Node gpu-npu-server-02 $NS "하스퍼 capability 수신(GPU measured/NPU 잠정)"

step "2) 고려대: 학습(compute) 제출 — annotation만(env는 KETI webhook이 주입)"
sub demo-train compute prefill 32
wait_run demo-train
echo "   webhook 주입된 env:"; kubectl get pod demo-train -n $NS -o jsonpath='{.spec.containers[0].env[?(@.name=="KRAKEN_WORKLOAD_CLASS")]}' 2>/dev/null; echo
sleep 5; cap

step "3) ★산: 서빙(memory) 추가 → 총할당률 간섭 구간 진입 + 비대칭 제어 자동 전환"
sub demo-serve memory decode 1
wait_run demo-serve
sleep 6
cap
ev ControlModeSwitched Pod demo-serve $NS "off→relaxed: victim(memory) released, aggressor(compute) armed"
ev InterferenceDetected Node gpu-npu-server-02 $NS "allocation_ratio≥1.40 경합 — 비대칭 제어로 victim 보호"

step "4) 워크로드 실행 → 오케스트로 지표(유지율) 충전 대기"
for i in $(seq 1 20); do
  kubectl logs demo-serve -n $NS 2>/dev/null | grep -q '"sps"' && break; sleep 3
done
sleep 2
cp $SYNC/result_demo-serve.json $SYNC/result_demo-serve.json 2>/dev/null
echo "   exporter /metrics (유지율 포함):"
curl -s http://localhost:9095/metrics 2>/dev/null | grep -E 'allocation_ratio|interference_zone|retention' | sed 's/^/     /'

step "5) 회수: 하스퍼 reclaim_pending → 감시 데몬 드레인 → reclaim_ready"
python3 -c "import json,glob;f=[x for x in glob.glob('$KRAKEN_CAP_DIR/GPU-*.json')][0];d=json.load(open(f));d['status']='reclaim_pending';json.dump(d,open(f,'w'),indent=2);print('   capability → reclaim_pending')"
for i in $(seq 1 15); do
  st=$(python3 -c "import json,glob;print(json.load(open([x for x in glob.glob('$KRAKEN_CAP_DIR/GPU-*.json')][0]))['status'])" 2>/dev/null)
  [ "$st" = "reclaim_ready" ] && { echo "   감시 데몬 회신: reclaim_ready (슬라이스 비움 완료)"; break; }
  sleep 2
done

T1=$(date +%s)
echo ""; echo "━━━ 데모 완료. 소요 $((T1-T0))초 ━━━"
