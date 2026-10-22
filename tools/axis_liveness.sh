#!/usr/bin/env bash
# axis_liveness.sh — 다섯 제어 축의 "실효"를 실측으로 증명한다 (Exp_73 Part C).
#
# 반복된 실패(O-3 MPS 무음 no-op, O-5 mem quota 미소비, RNGD set_device 무시,
# GPU feeder 미배선…)의 공통 형태 = "코드에 있고 설정도 들어가는데 아무 일도 안 일어남".
# → 각 축마다 살아있음의 관측 지점을 정하고, 확인 안 되면 그 축은 FAIL(경고 아님).
#
# 사용: tools/axis_liveness.sh          (전 축)
#       tools/axis_liveness.sh space    (한 축만)
# 종료코드: 0=전 축 PASS, 1=하나라도 FAIL. RESTORE.md §F 에서 재부팅 후 호출.
set -u
NS=default
IMG=nvidia/cuda:12.6.0-base-ubuntu22.04
BIN=/root/exp71-bin              # cuinit/alloc/computeloop/membound (호스트)
# [Exp_112 1부] ★compute 부하 burnloop2 → computeloop 교체.
#   burnloop2 는 Xid 31 MMU Fault(FAULT_PDE ACCESS_TYPE_VIRT_READ)를 낸다 — Exp_110
#   에서 Xid 62건 중 48건이 여기서 나왔고(membound 은 0건), 그 결과 cudaMalloc 이
#   "device busy" 로 실패해 파드가 조기 종료 → 짝짓기 축이 arm=1 release=0 으로 실패했다.
#   computeloop(reports/Exp_112_axis_workload_swap/src/computeloop.cu)은 경계 안전
#   grid-stride + 레지스터 FMA 루프라 fault 경로가 없다. 인자 6000 = 커널 1회 ~1.02ms
#   (burnloop2 800000 의 1.2ms 와 같은 granularity — 게이트 입도를 유지한다).
FAIL=0
only="${1:-all}"

# [Exp_143 1-C] 롤아웃 직후 정착 대기 — 플러그인 파드가 어리면 판정이 흔들린다.
#   실측: Exp_140·141 에서 롤아웃 직후 NPU 축이 거짓 FAIL(빈 출력·일시 레이스),
#   Exp_142 에서 120초 정착 대기 후 실행하자 전 축 단번 PASS. 값 120s = Exp_142.
#   파드 나이만큼 차감해 필요할 때만 기다린다. SETTLE_S=0 으로 끌 수 있다.
SETTLE_S=${SETTLE_S:-120}
if [ "$SETTLE_S" != "0" ]; then
  AGE=$(kubectl -n kube-system get pod -l app=ketris-device-plugin-lsu \
        -o jsonpath='{.items[0].status.startTime}' 2>/dev/null)
  if [ -n "$AGE" ]; then
    EL=$(( $(date +%s) - $(date -d "$AGE" +%s 2>/dev/null || echo 0) ))
    if [ "$EL" -lt "$SETTLE_S" ]; then
      W=$((SETTLE_S - EL))
      echo "[정착 대기] plugin 파드 기동 ${EL}s 경과 — ${W}s 대기 (Exp_142 근거 120s)"
      sleep "$W"
    fi
  fi
fi

pass(){ echo "  ✅ PASS: $1"; }
fail(){ echo "  ❌ FAIL: $1"; FAIL=1; }

: > /tmp/axl_create_err.log 2>/dev/null   # [Exp_112] 파드 생성 stderr 수집

# [Exp_112] ★부하 바이너리 사전 점검 — 조용한 실패 방지.
#   nvcc 기본은 cudart **정적 링크**이고, 그러면 LD_PRELOAD 인터포지션이 걸리지 않아
#   libbless 가 초기화조차 되지 않는다(오류 메시지 0). 프로그램은 정상 동작하므로
#   "측정이 안 되는" 것으로만 보인다 — 실측으로 한 번 당했다.
for b in computeloop membound; do
  [ -x "$BIN/$b" ] || { echo "❌ 부하 바이너리 없음: $BIN/$b"; exit 2; }
  ldd "$BIN/$b" 2>/dev/null | grep -q 'libcudart\.so'     || { echo "❌ $b 가 cudart 를 정적 링크했다 — LD_PRELOAD 인터포지션 불가.";          echo "   재빌드: nvcc --cudart=shared (reports/Exp_112_axis_workload_swap/src/build.sh)"; exit 2; }
done
cleanup(){ kubectl -n $NS delete pod axl-space axl-mem axl-npu axl-cdi axl-time-hi axl-time-lo axl-cls-c axl-cls-m --now >/dev/null 2>&1; }

# [Exp_110 R-1] ★축 간 자원 회수 대기.
#   `delete --now` 는 즉시 반환하지만 kubelet 종료·device-plugin 반납·MPS 클라이언트
#   정리는 그 뒤에 일어난다. 다음 축이 곧바로 CUDA 컨텍스트를 만들면
#   `cudaMalloc: CUDA-capable device(s) is/are busy or unavailable` 로 죽는다(실측:
#   compute 파드 EXIT=3, 소켓 미출현 → 짝짓기 축이 arm=1 release=0 으로 FAIL).
#   축 단독 실행은 5/5 통과하는데 6축 연속에서만 간헐 실패하던 것의 실제 기전이다.
#   합격 기준을 낮추는 것이 아니라 하네스가 스스로 만든 경합을 없앤다.
drain(){ # 살아 있는 CUDA 파드 소멸 + feeder 테넌트 회수까지 대기 (최대 60초)
  #   ★Succeeded/Failed 파드 객체는 자원을 쥐고 있지 않으므로 세지 않는다.
  #     Running/Pending 만이 MPS 클라이언트를 점유한다.
  local n u
  for i in $(seq 1 30); do
    n=$(kubectl -n $NS get pods --no-headers 2>/dev/null \
        | awk '$1 ~ /^axl-/ && ($3=="Running" || $3=="Pending" || $3=="ContainerCreating") ' | wc -l)
    u=$(curl -s --max-time 3 http://localhost:8090/feeder/status 2>/dev/null \
        | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('tenants',{})))" 2>/dev/null)
    [ "${n:-1}" = 0 ] && [ "${u:-1}" = 0 ] && { sleep 4; return 0; }
    sleep 2
  done
  echo "  [경고] drain 미완 — 살아있는 파드 ${n}개 / feeder 테넌트 ${u}개 잔존 상태로 다음 축 진행"
  return 0
}
trap cleanup EXIT

# ── 축 1: 공간 (MPS pct) — init 로그 sm_limit=delegated-mps-env (FALLBACK 아님) ──
if [ "$only" = all ] || [ "$only" = space ]; then
echo "[공간/MPS] sm_limit 실효값 (설정 아닌 로그로 판정)"
kubectl -n $NS run axl-space --image=$IMG --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"'$IMG'","command":["bash","-c","/bin/x/cuinit; sleep 3"],
"resources":{"limits":{"cpu":"1","memory":"1Gi","keti.re.kr/lsu":"36"}},
"volumeMounts":[{"name":"x","mountPath":"/bin/x","readOnly":true}]}],
"volumes":[{"name":"x","hostPath":{"path":"'$BIN'","type":"Directory"}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  for i in $(seq 1 30); do kubectl -n $NS logs axl-space 2>/dev/null | grep -q "init:" && break; sleep 2; done
  log=$(kubectl -n $NS logs axl-space 2>/dev/null)
  if echo "$log" | grep -q "sm_limit=delegated-mps-env"; then pass "공간: delegated-mps-env"
  elif echo "$log" | grep -q "FALLBACK-unlimited"; then fail "공간: FALLBACK-unlimited — MPS 경로 끊김(무음 no-op)"
  else fail "공간: init 로그 없음 (파드 미기동?)"; fi
fi

# ── 축 2: 메모리 (쿼터) — 초과 cudaMalloc REJECTED ──
if [ "$only" = all ] || [ "$only" = mem ]; then
echo "[메모리/쿼터] 초과 할당 거부"
kubectl -n $NS run axl-mem --image=$IMG --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"'$IMG'","command":["bash","-c","/bin/x/alloc; sleep 3"],
"resources":{"limits":{"cpu":"1","memory":"1Gi","keti.re.kr/lsu":"18"}},
"volumeMounts":[{"name":"x","mountPath":"/bin/x","readOnly":true}]}],
"volumes":[{"name":"x","hostPath":{"path":"'$BIN'","type":"Directory"}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  for i in $(seq 1 30); do kubectl -n $NS logs axl-mem 2>/dev/null | grep -q "ALLOC" && break; sleep 2; done
  if kubectl -n $NS logs axl-mem 2>/dev/null | grep -q "REJECTED"; then pass "메모리: 쿼터 초과 REJECTED"
  else fail "메모리: REJECTED 없음 — 쿼터 미집행(O-5 계열)"; fi
fi

# ── 축 3: 시간 (크레딧) — feeder 등록 + occupancy + ★LSU 비례(Exp_74) ──
if [ "$only" = all ] || [ "$only" = time ]; then
echo "[시간/크레딧] feeder 자동 등록 + occupancy + LSU 비례 몫"
kubectl -n $NS delete pod axl-space axl-mem --now >/dev/null 2>&1; drain
# LSU 90:30(=3:1) 두 테넌트 → observed_share 비가 ~3 이어야 시간축이 LSU 비례로 산다.
for spec in "hi 90" "lo 30"; do set -- $spec
  kubectl -n $NS run axl-time-$1 --image=$IMG --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"'$IMG'","command":["bash","-c","/bin/x/computeloop 45 6000; rc=$?; [ $rc = 0 ] || echo DIED rc=$rc"],
"resources":{"limits":{"cpu":"2","memory":"2Gi","keti.re.kr/lsu":"'$2'"}},
"volumeMounts":[{"name":"x","mountPath":"/bin/x","readOnly":true}]}],
"volumes":[{"name":"x","hostPath":{"path":"'$BIN'","type":"Directory"}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  sleep 5
done
  sleep 12
  n=$(curl -s http://localhost:8090/feeder/status | python3 -c "import json,sys;print(len(json.load(sys.stdin)['tenants']))" 2>/dev/null)
  curl -s http://localhost:8090/feeder/occupancy >/dev/null; sleep 3
  # [Exp_112] ★"비=0" 은 세 가지 다른 원인을 하나로 뭉개고 있었다(T-5 여섯 번째 사례):
  #   (ㄱ) occupancy 에 테넌트가 1개만 잡힘  (ㄴ) 한쪽 charged=0  (ㄷ) curl/JSON 파싱 실패
  #   2>/dev/null 이 (ㄷ)을 빈 문자열로 만들고 ${ratio:-0} 이 그것을 0 으로 바꿔
  #   "균등 또는 미배선" 이라는 엉뚱한 진단을 냈다. 원인을 구분해 내보낸다.
  occ_raw=$(curl -s --max-time 5 http://localhost:8090/feeder/occupancy)
  ratio=$(printf '%s' "$occ_raw" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('PARSE_FAIL'); sys.exit(0)
sh=d.get('observed_share',{})
if len(sh)<2: print('ONLY_%d_TENANT'%len(sh)); sys.exit(0)
v=sorted(sh.values(),reverse=True)
if v[1]<=0: print('ZERO_SHARE'); sys.exit(0)
print(round(v[0]/v[1],2))")
  # 비 2.4~3.6 이면 LSU 3:1 비례 성립(±20% 허용 — MPS 캡 상호작용 감안)
  # [Exp_112] 허용 구간은 env 로 받는다 — 새 부하(computeloop) 기준선을 근거로 기입.
  ok_ratio=$(printf '%s' "${ratio:-}" | python3 -c "
import sys
t=sys.stdin.read().strip()
try: r=float(t)
except ValueError: print(0); sys.exit(0)
print(1 if ${RATIO_LO:-2.4} <= r <= ${RATIO_HI:-3.6} else 0)")
  if [ "${n:-0}" -ge 2 ] && [ "${ok_ratio:-0}" = 1 ]; then pass "시간: feeder $n + occupancy + LSU 비례(비=$ratio, 기대 3.0)"
  else fail "시간: feeder=$n / LSU 비례 실패(비=$ratio, 기대~3.0, 허용 ${RATIO_LO:-2.4}~${RATIO_HI:-3.6})"
       # [Exp_110] 실패 시 파드 조기 종료 여부를 드러낸다(T-5 규칙 — 조용한 실패 금지)
       for pp in axl-time-hi axl-time-lo; do
         echo "    [$pp] phase=$(kubectl -n $NS get pod $pp -o jsonpath='{.status.phase}' 2>/dev/null) $(kubectl -n $NS logs $pp 2>/dev/null | grep -E 'DIED|ERR |busy' | head -2)"
       done
       echo "    occupancy: $occ_raw"; fi
  kubectl -n $NS delete pod axl-time-hi axl-time-lo --now >/dev/null 2>&1
  drain
fi

# ── 축 6: relaxed 자동 분류 (Exp_75) — 이종 선언 시 victim 해제 ──
if [ "$only" = all ] || [ "$only" = classify ]; then
echo "[relaxed 분류] 이종(compute+memory) 선언 → memory 자동 해제"
for spec in "c compute computeloop" "m memory membound"; do set -- $spec
  args='["bash","-c","/bin/x/'$3' 40 6000; rc=$?; [ $rc = 0 ] || echo DIED rc=$rc"]'; [ "$3" = membound ] && args='["bash","-c","/bin/x/membound 40 2048 4; rc=$?; [ $rc = 0 ] || echo DIED rc=$rc"]'
  kubectl -n $NS run axl-cls-$1 --image=$IMG --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"'$IMG'","command":'"$args"',
"env":[{"name":"KRAKEN_WORKLOAD_CLASS","value":"'$2'"}],
"resources":{"limits":{"cpu":"2","memory":"2Gi","keti.re.kr/lsu":"80"}},
"volumeMounts":[{"name":"x","mountPath":"/bin/x","readOnly":true}]}],
"volumes":[{"name":"x","hostPath":{"path":"'$BIN'","type":"Directory"}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  sleep 5
done
  sleep 12
  # feeder armed: compute=True, memory=False 이어야 relaxed 자동 성립
  st=$(curl -s http://localhost:8090/feeder/status)
  narm=$(echo "$st" | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for v in d['tenants'].values() if v['armed']))" 2>/dev/null)
  nrel=$(echo "$st" | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for v in d['tenants'].values() if not v['armed']))" 2>/dev/null)
  if [ "${narm:-0}" -ge 1 ] && [ "${nrel:-0}" -ge 1 ]; then pass "relaxed 자동: arm=$narm(compute) release=$nrel(memory victim)"
  else fail "relaxed 자동 실패: arm=$narm release=$nrel (이종인데 victim 미해제 — 분류 끊김)"; fi
  kubectl -n $NS delete pod axl-cls-c axl-cls-m --now >/dev/null 2>&1
  drain
fi

# ── 축 4: NPU PE — 부여 세트 = 블록, 블록 밖 미노출 ──
if [ "$only" = all ] || [ "$only" = npu ]; then
echo "[NPU PE] 정렬 블록 부여, 블록 밖 미노출"
kubectl -n $NS run axl-npu --image=alpine --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"alpine","command":["sh","-c","ls /dev/rngd | tr \"\\n\" \" \"; sleep 3"],
"resources":{"limits":{"cpu":"1","memory":"512Mi","keti.re.kr/npu-pe":"4"}}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  # [Exp_140] ★NPU 인덱스 하드코딩 제거. 엣지팀 드라이버 재설치 후 RNGD 가
  #   npu1 → npu0 으로 재열거됐다(호스트 /dev/rngd 실측 — npu1 노드 0개).
  #   플러그인 discovery 는 동적이라 정상인데 이 판정만 npu1 을 못박아
  #   Exp_139·140 에서 거짓 FAIL 을 냈다. 인덱스를 파드 수령 목록에서 뽑는다.
  for i in $(seq 1 20); do kubectl -n $NS logs axl-npu 2>/dev/null | grep -qE 'npu[0-9]+pe' && break; sleep 2; done
  nodes=$(kubectl -n $NS logs axl-npu 2>/dev/null)
  idx=$(printf '%s' "$nodes" | grep -oE 'npu[0-9]+pe' | head -1 | grep -oE '[0-9]+')
  # 4PE 요청 → pe0-3 블록만, pe4~7 미노출
  if [ -n "$idx" ] && echo "$nodes" | grep -qw "npu${idx}pe0" && ! echo "$nodes" | grep -qw "npu${idx}pe7"; then
    pass "NPU: 블록 노출, 블록 밖 미노출 (npu${idx})"
  else fail "NPU: 블록 격리 실패 ($nodes)"; fi
fi

# ── 축 5: 접근 경계 (CDI) — 미요청 파드 GPU 미노출 ──
if [ "$only" = all ] || [ "$only" = cdi ]; then
echo "[접근경계/CDI] 리소스 미요청 파드는 GPU 불가"
kubectl -n $NS run axl-cdi --image=$IMG --restart=Never --overrides='
{"spec":{"containers":[{"name":"t","image":"'$IMG'","command":["bash","-c","nvidia-smi -L 2>&1 || echo NO-GPU; sleep 3"],
"resources":{"limits":{"cpu":"1","memory":"512Mi"}}}]}}' >/dev/null 2>>/tmp/axl_create_err.log || echo "  [경고] 파드 생성 실패 — /tmp/axl_create_err.log 확인"
  for i in $(seq 1 20); do kubectl -n $NS logs axl-cdi 2>/dev/null | grep -qE "NO-GPU|GPU 0" && break; sleep 2; done
  if kubectl -n $NS logs axl-cdi 2>/dev/null | grep -q "NO-GPU"; then pass "접근경계: 미요청 파드 GPU 미노출"
  else fail "접근경계: 미요청 파드가 GPU 접근(구멍)"; fi
fi

echo
[ $FAIL -eq 0 ] && echo "=== 전 축 PASS ===" || echo "=== FAIL 있음 (위 ❌) ==="
exit $FAIL
