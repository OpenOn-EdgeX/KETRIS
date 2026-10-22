#!/bin/bash
# 이기종(GPU+NPU) 통합 데모 (Exp_83 Part C). 한 노드에서 GPU 슬라이스 + NPU PE 블록 동시 배정.
# 전제: factor 1.5, controller /capacity(:8090)·exporter(:9095, FURIOSA_SMI), kraken-demo NS, kraken-npu:qwen 이미지.
set -u
ROOT=/root/workspace/pooling/prism; DEMO=$ROOT/integration_demo
NS=kraken-demo; GSYNC=/root/kraken-demo-sync; NSYNC=/root/kraken-npu-sync
export KRAKEN_CAP_DIR=/var/lib/kraken/capability
SMI=/usr/bin/furiosa-smi
ev(){ python3 $DEMO/orchestro_stub/emit_event.py "$@" >/dev/null 2>&1; }
step(){ echo ""; echo "━━━ [$(date +%H:%M:%S)] $1"; }
gsub(){ sed "s|@@NAME@@|$1|;s|@@NS@@|$NS|;s|@@CLASS@@|$2|;s|@@BATCH@@|$4|;s|@@LSU@@|133|;s|@@STAGE@@|$3|" \
    $DEMO/korea_stub/workload_pod.tmpl.yaml | kubectl apply -f - >/dev/null; }
nsub(){ sed "s|@@NAME@@|$1|;s|@@NS@@|$NS|;s|@@PE@@|$2|;s|@@DUR@@|60|" \
    $DEMO/npu_stub/npu_pod.tmpl.yaml | kubectl apply -f - >/dev/null; }
wait_run(){ for i in $(seq 1 60); do kubectl get pod $1 -n $NS --no-headers 2>/dev/null|grep -q Running && return; sleep 2; done; }
cap(){ curl -s http://localhost:8090/capacity | python3 -c "import json,sys;d=json.load(sys.stdin);g=d['devices'][0];c=g['capacity'];z='간섭' if c['allocation_ratio']>=1.4 else('전이' if c['allocation_ratio']>=1.0 else '무간섭');print('   GPU 총할당률=%.2f (%s) armed=%s | NPU 가용 %d PE'%(c['allocation_ratio'],z,[(s['tenant'][-8:],s['armed']) for s in g['slices']],d['npu']['available_cores']))"; }
T0=$(date +%s); rm -f $GSYNC/* $NSYNC/* 2>/dev/null; mkdir -p $GSYNC $NSYNC

step "1) 하스퍼: 물리 자원 명세 — GPU + NPU 둘 다"
python3 $DEMO/hasper_stub/hasper_capability_agent.py | sed 's/^/   /'
curl -s http://localhost:8090/capacity | python3 -c "import json,sys;d=json.load(sys.stdin);g=d['devices'][0]['capacity'];n=d['npu'];print('   GPU 광고 %d LSU(factor %.1f) | NPU 가용 %d PE, 허용블록 %s'%(g['advertised_lsu'],g['overcommit_factor'],n['available_cores'],n['request_rule']['allowed_blocks']))"

step "2) NPU 서빙 2인스턴스 (PE 0-3 / 4-7, 같은 모델 — 이종 동거 불가) + GPU 학습/서빙"
nsub npu-serve-a 4; nsub npu-serve-b 4
gsub gpu-train compute prefill 32; gsub gpu-serve memory decode 1
echo "   4워크로드 스케줄 대기(NPU 로드 ~6s, GPU 로드 ~25s)..."
for p in npu-serve-a npu-serve-b gpu-train gpu-serve; do wait_run $p; done
ev PEBlockAllocated Node gpu-npu-server-02 $NS "NPU PE 0-3, 4-7 배정 (커널 강제 격리)"

step "3) 정상상태 진입 대기 (NPU 추론 + GPU 실행)"
sleep 22
echo "   한 노드 동시 배정 (GPU 슬라이스 + NPU PE 블록):"
kubectl get pods -n $NS --no-headers 2>/dev/null | grep -E 'gpu-|npu-serve' | sed 's/^/     /'
echo "   ★호스트 furiosa-smi ps (NPU 실 PE 점유):"; $SMI ps 2>/dev/null | grep -E 'npu1|CMD' | sed 's/^/     /'

step "4) ★GPU 총할당률 간섭 구간 + 비대칭 제어 (NPU는 공간분할만, 시간분할 미구현)"
cap
ev ControlModeSwitched Pod gpu-serve $NS "GPU off→relaxed: victim released, aggressor armed (NPU 시간분할 미구현)"
ev InterferenceDetected Node gpu-npu-server-02 $NS "GPU allocation≥1.40 경합 — 비대칭 제어"

step "5) 오케스트로: GPU 슬라이스 + NPU PE 맵 동시 노출 (이기종 가시화)"
curl -s http://localhost:9095/metrics 2>/dev/null | grep -E 'allocation_ratio|interference_zone|npu_pe_block|npu_cores_available|slice_armed' | sed 's/^/     /'

step "6) 회수: NPU PE 블록 해제 + GPU 슬라이스 비움"
kubectl delete pods -n $NS --all --now >/dev/null 2>&1
sleep 3; n=$($SMI ps 2>/dev/null | grep -c npu_worker); echo "   NPU 잔여 프로세스: $n (PE 해제)"
ev PEBlockReleased Node gpu-npu-server-02 $NS "NPU PE 블록 해제"

T1=$(date +%s); echo ""; echo "━━━ 이기종 데모 완료. 소요 $((T1-T0))초 ━━━"
