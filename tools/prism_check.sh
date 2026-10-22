#!/bin/bash
# prism_check.sh — "내 파드에 PRISM 이 붙었나, 얼마를 받았나" 한 화면 확인 (Exp_164)
#
# 사용법: tools/prism_check.sh <파드이름> [-n 네임스페이스]
#
# ★읽기 전용이다 — 조회(kubectl get/exec printenv·ls, curl GET)만 하고 아무것도 바꾸지 않는다.
# 필요 권한: kubectl get/exec. controller 조회(:8090)는 232 에서 실행할 때만 되고,
# 안 되면 그 항목만 "확인 불가"로 표시하고 계속 간다.
set -u
NS=default
POD=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n) NS="$2"; shift 2;;
    -h|--help) grep '^# ' "$0" | head -8; exit 0;;
    *) POD="$1"; shift;;
  esac
done
[ -z "$POD" ] && { echo "사용법: $0 <파드이름> [-n 네임스페이스]"; exit 2; }

CTRL="${PRISM_CTRL_URL:-http://127.0.0.1:8090}"
LSU_RES="keti.re.kr/lsu"
NPU_RES="keti.re.kr/npu-pe"

J=$(kubectl get pod "$POD" -n "$NS" -o json 2>/dev/null)
[ -z "$J" ] && { echo "판정: 확인 불가 — 파드 $NS/$POD 를 찾을 수 없음"; exit 1; }

# ── 파드 spec 에서 요청량·annotation·상태 ──
eval "$(echo "$J" | python3 -c '
import json,sys
p=json.load(sys.stdin)
lim={}
for c in p["spec"].get("containers",[]):
    for src in ("limits","requests"):
        for k,v in ((c.get("resources") or {}).get(src) or {}).items():
            if k.startswith("keti.re.kr/"): lim.setdefault(k,v)
ann=(p["metadata"].get("annotations") or {})
pre="kraken.keti.re.kr/"
print("LSU_REQ=%s" % lim.get("keti.re.kr/lsu", 0))
print("NPU_REQ=%s" % lim.get("keti.re.kr/npu-pe", 0))
print("PHASE=%s" % p["status"].get("phase", "?"))
print("ANN_CLASS=%s" % ann.get(pre + "workload-class", ""))
print("ANN_STAGE=%s" % ann.get(pre + "pipeline-stage", ""))
print("ANN_PRIO=%s" % ann.get(pre + "priority", ""))
')"

echo "=== PRISM 확인: $NS/$POD (상태 $PHASE)"
echo "[요청] $LSU_RES=$LSU_REQ  $NPU_RES=$NPU_REQ"
echo "[annotation] workload-class=${ANN_CLASS:-없음}  pipeline-stage=${ANN_STAGE:-없음}  priority=${ANN_PRIO:-없음}"

if [ "$LSU_REQ" = "0" ] && [ "$NPU_REQ" = "0" ]; then
  echo "판정: ❌ PRISM 적용 안 됨 — $LSU_RES 도 $NPU_RES 도 요청하지 않음 (resources.limits 에 넣어야 함)"
  exit 0
fi

if [ "$PHASE" = "Pending" ]; then
  echo "[대기 사유]"
  kubectl get events -n "$NS" --field-selector "involvedObject.name=$POD" \
    -o jsonpath='{range .items[*]}{.reason}: {.message}{"\n"}{end}' 2>/dev/null | tail -3 | sed 's/^/  /'
  echo "판정: ⏳ Pending — 위 사유 확인 (노출량 초과면 요청을 줄이거나 다른 작업 종료 대기)"
  exit 0
fi

# ── 파드 안 적용값 (exec printenv — Running 일 때만) ──
DEVID=""
if [ "$PHASE" = "Running" ]; then
  ENVDUMP=$(kubectl exec -n "$NS" "$POD" -- printenv 2>/dev/null)
  if [ -z "$ENVDUMP" ]; then
    echo "[적용값] 확인 불가 — kubectl exec 실패 (쉘 없는 이미지?)"
  else
    g(){ echo "$ENVDUMP" | grep "^$1=" | head -1 | cut -d= -f2-; }
    DEVID=$(g KRAKEN_TENANT)
    if [ "$LSU_REQ" != "0" ]; then
      echo "[적용값/GPU] 테넌트ID=$DEVID"
      echo "  MPS 연산 비율: $(g CUDA_MPS_ACTIVE_THREAD_PERCENTAGE)%  (가상 SM $(g KRAKEN_VIRTUAL_SM)/188)"
      echo "  메모리 할당량: $(g BLESS_MEM_QUOTA_MB) MB"
      echo "  workload-class env: $(g KRAKEN_WORKLOAD_CLASS)  priority env: $(g KRAKEN_PRIORITY)"
      echo "  빠른 해제: BLESS_FAST_TEARDOWN=$(g BLESS_FAST_TEARDOWN)"
      PRELOAD=$(g LD_PRELOAD)
      SOCKS=$(kubectl exec -n "$NS" "$POD" -- sh -c 'ls /tmp/bless-*.sock 2>/dev/null' 2>/dev/null)
      if echo "$PRELOAD" | grep -q libbless; then
        if [ -n "$SOCKS" ]; then
          echo "  libbless: 적재+초기화 확인 (소켓 $SOCKS)"
        else
          echo "  libbless: LD_PRELOAD 설정됨, 소켓 아직 없음 (CUDA 초기화 전이면 정상)"
        fi
      else
        echo "  libbless: ❌ LD_PRELOAD 에 없음 — 파드가 LD_PRELOAD 를 valueFrom 으로 덮었거나 비정상"
      fi
    fi
    if [ "$NPU_REQ" != "0" ]; then
      echo "[적용값/NPU] 장치=$(g KETI_NPU_DEVICES)  PE범위=$(g KETI_NPU_PE_RANGE)  PE수=$(g KETI_NPU_PE_COUNT)"
    fi
  fi
fi

# ── controller 조회 (노드 전체 비율 + 시간 몫) ──
CAP=$(curl -s --max-time 3 "$CTRL/capacity" 2>/dev/null)
if [ -n "$CAP" ]; then
  echo "$CAP" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for dev in d.get("devices",[]):
    if dev.get("kind")=="gpu":
        c=dev["capacity"]
        print("[노드 전체] GPU 할당 %s/%s LSU (노출량 대비 %.0f%% · 물리 대비 %.0f%%)" % (
            c["allocated_lsu"], c["advertised_lsu"],
            100.0*c["allocated_lsu"]/max(1,c["advertised_lsu"]), c["allocation_ratio"]*100))
'
else
  echo "[노드 전체] 확인 불가 — controller($CTRL) 접근 안 됨 (232 에서 실행하거나 PRISM_CTRL_URL 지정)"
fi

VERDICT=""
if [ -n "$DEVID" ]; then
  FS=$(curl -s --max-time 3 "$CTRL/feeder/status" 2>/dev/null)
  if [ -n "$FS" ]; then
    LINE=$(echo "$FS" | python3 -c '
import json,sys
d=json.load(sys.stdin); dev="'"$DEVID"'"
t=(d.get("tenants") or {}).get(dev)
if not t:
    print("MISSING"); raise SystemExit
armed=t.get("armed"); ts=t.get("target_share"); ratio=t.get("ratio")
n_armed=sum(1 for x in d["tenants"].values() if x.get("armed"))
n_all=len(d["tenants"])
guar=round(float(ratio)/max(1.0,sum(float(x["ratio"]) for x in d["tenants"].values() if x.get("armed"))),4) if armed else None
if armed:
    print(f"ARMED|보장 점유 비율 {guar} (적용값 {ts}) · 시분할 적용 중 · 동거 테넌트 {n_all}개(제한 {n_armed})")
else:
    print(f"RELEASED|시분할 해제됨(공동 배치 완화 — 지켜줄 쪽으로 판정) · 동거 테넌트 {n_all}개")
')
    case "$LINE" in
      MISSING) echo "[시간 몫] feeder 미등록 — CUDA 초기화 전이면 잠시 후 재확인";;
      ARMED*|RELEASED*) echo "[시간 몫] ${LINE#*|}";;
    esac
  else
    echo "[시간 몫] 확인 불가 — controller 접근 안 됨"
  fi
fi

# ── 판정 ──
if [ "$PHASE" != "Running" ]; then
  echo "판정: ⏳ 파드가 Running 이 아님($PHASE) — Running 후 다시 확인"
elif [ "$LSU_REQ" != "0" ] && [ -n "$DEVID" ]; then
  echo "판정: ✅ PRISM 적용됨 (GPU 분할 — 위 적용값으로 운용 중)"
elif [ "$NPU_REQ" != "0" ]; then
  echo "판정: ✅ PRISM 적용됨 (NPU PE 블록 — 위 장치로 운용 중)"
else
  echo "판정: ⚠️ LSU 요청은 있으나 적용값 확인 불가 — 위 항목의 사유 참조"
fi
