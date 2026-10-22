#!/bin/bash
# [Exp_106 1-C] overcommit factor 교체 — 개명 후(kraken-*) 이름 기준.
#   ★기존 exp89_setfactor.sh 는 prism-lsu-catalog / prism-device-plugin-lsu 를 가리켜
#     개명 후 라이브에서는 동작하지 않는다(Exp_103 I-5 ConfigMap 중복과 연결).
#   ★Exp_93 교훈: DS 재시작 후 allocatable 이 돌아와도 kubelet 이 device 를 healthy 로
#     재등록하기 전에는 배치가 거부된다. allocatable 확인 + 여유 대기.
set -u
F=$1
CM=kraken-lsu-catalog; DS=kraken-device-plugin-lsu; NODE=gpu-npu-server-02
cur=$(kubectl get cm $CM -n kube-system -o jsonpath='{.data.catalog\.json}')
[ -n "$cur" ] || { echo "★catalog CM 조회 실패"; exit 1; }
new=$(echo "$cur" | python3 -c "import json,sys;d=json.load(sys.stdin);d['overcommit_factor']=float('$F');print(json.dumps(d))")
kubectl create cm $CM -n kube-system --from-literal=catalog.json="$new" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl rollout restart ds $DS -n kube-system >/dev/null
kubectl rollout status ds $DS -n kube-system --timeout=120s >/dev/null 2>&1
exp=$(python3 -c "print(round(178*float('$F')))")
a=""
for i in $(seq 1 40); do
  a=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
  [ "$a" = "$exp" ] && break; sleep 3
done
sleep 8   # device healthy 재등록 여유
echo "factor=$F → allocatable lsu=$a (기대 $exp)"

# [Exp_143 1-B] ★CM 만 패치하면 다음 `kubectl apply -f daemonset-lsu.yaml` 이
#   매니페스트 내장 CM 으로 **배율을 되돌린다** — Exp_142 에서 실제로 당했다
#   (④ 시험 1차 무효, 무경보. Exp_122 "apply 가 태그를 되돌림"과 동형).
#   그래서 정본(lsu/catalog.json)과 매니페스트 CM 도 함께 고친다.
#   임시 실험(스윕 등)으로 레포를 건드리기 싫으면 SETFACTOR_CM_ONLY=1 —
#   단 그 상태에서는 apply 금지이며 deploy_verify ④ 가 불일치를 잡는다.
REPO=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${SETFACTOR_CM_ONLY:-0}" = "1" ]; then
  echo "⚠ SETFACTOR_CM_ONLY=1 — 라이브 CM 만 바꿨다. 정본=$(
    python3 -c "import json;print(json.load(open('$REPO/lsu/catalog.json'))['overcommit_factor'])"
  ) 와 불일치 상태다. 이 상태에서 kubectl apply 를 치면 배율이 되돌아간다(Exp_142)."
else
  python3 - "$REPO" "$F" <<'PYEOF'
import json, re, sys
repo, f = sys.argv[1], float(sys.argv[2])
p = f"{repo}/lsu/catalog.json"
d = json.load(open(p)); d["overcommit_factor"] = f
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
p2 = f"{repo}/device-plugin/deploy/daemonset-lsu.yaml"
s = open(p2).read()
s2, n = re.subn(r'"overcommit_factor":\s*[0-9.]+', f'"overcommit_factor": {f}', s)
open(p2, "w").write(s2)
print(f"정본 lsu/catalog.json + 매니페스트 CM({n}건) → {f} 동기화. "
      f"★커밋해야 정본이 된다 — 커밋 없이 두면 다음 사람이 diff 로 발견한다.")
PYEOF
fi
