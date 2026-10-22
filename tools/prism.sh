#!/bin/bash
# [on_off] PRISM(KRAKEN) 내림/올림 — 232 를 다른 용도로 잠시 쓸 때.
#
# ── 사용 설명 ────────────────────────────────────────────────────────────────
#   tools/prism.sh status        올라감/내려감/어중간 을 한 화면에
#   tools/prism.sh down          KRAKEN 을 내린다 (~1분)
#   tools/prism.sh down --k8s-gpu   + 표준 nvidia.com/gpu 광고를 켠다
#   tools/prism.sh up            내리기 전 상태로 복귀 + 검증 (~4분, 6축 포함)
#
# 언제 쓰나: 232 GPU·NPU 를 KRAKEN 없이(호스트 직접 or 표준 K8s) 써야 할 때.
# down 뒤 가능한 것: 호스트에서 CUDA 직접 실행(MPS 없음·GPU 전량), NPU 직접 사용,
#   (--k8s-gpu 시) nvidia.com/gpu=1 파드. 일반 파드 생성도 webhook 지연 없이 즉시.
# down 뒤 안 되는 것: keti.re.kr/lsu·npu-pe 요청(광고 소멸), 몫·메모리·시간 제어.
# up 뒤 확인: 스크립트가 deploy_verify + 6축 + 노출 240/8 + down 직전 기록 대조를
#   자동으로 한다. "UP COMPLETE" 가 뜨면 끝. 실패 시 멈춘 단계와 다음 행동을 출력.
# 실패하면 볼 곳: /var/lib/kraken/prism_updown/ (down 직전 기록·caBundle 백업),
#   kubectl -n kube-system get pods -l app=ketris-device-plugin-lsu,
#   tools/deploy_verify.sh 출력.
#
# ── 원칙 ────────────────────────────────────────────────────────────────────
#   · LSU 계열 파드가 있으면 down 거부(--force 로만 무시 — armed 파드는 제어
#     경로가 사라지면 크레딧 동결된다, Exp_140). 남의 파드는 절대 지우지 않는다.
#   · MutatingWebhookConfiguration 은 반드시 함께 지운다 — 설정만 남으면 모든
#     파드 생성이 죽은 엔드포인트 타임아웃을 기다린다.
#   · 지우지 않는 것: CRD·RBAC·ConfigMap·Secret·/var/lib/kraken 배치물
#     (LSU 요청이 없으면 무영향 — up 이 다시 쓴다).
#   · 재부팅 경로 없음. 233·타 네임스페이스·control-plane 불가침. 멱등.
#   · 구성요소는 라벨/셀렉터로 찾는다(사이드카가 늘어도 스크립트 무수정).
set -u
cd "$(dirname "$0")/.."
NSK=kube-system
NODE=gpu-npu-server-02
STATE=/var/lib/kraken/prism_updown
DS_SEL="app=ketris-device-plugin-lsu"
WH_SEL="app=ketris-webhook"
MWC=ketris-env-webhook
VAP=kraken-npu-fit
NV_DS=nvidia-device-plugin-daemonset
NV_OFF="disabled-kraken-v2-test"          # Exp_70: nodeSelector 로 광고 비활성
T0=$(date +%s)
say(){ echo "[prism] $*"; }
die(){ echo "[prism][실패] $1"; echo "[prism] 다음: ${2:-위 메시지의 원인을 해소하고 재실행 (멱등)}"; exit 1; }

lsu_pods(){
  kubectl get pods -A -o json 2>/dev/null | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    for c in p['spec']['containers']:
        lim=(c.get('resources') or {}).get('limits') or {}
        if any(k in ('keti.re.kr/lsu','keti.re.kr/npu-pe','keti.re.kr/remote-lsu') for k in lim):
            print(p['metadata']['namespace'], p['metadata']['name']); break"
}

# ★식별은 실행 파일 기준(pidof = /proc/*/exe) — pgrep -f 는 호출자 셸의 cmdline 에
#   패턴이 있으면 그 셸을 잡아 kill 이 호출자를 죽인다(on_off 검증에서 여섯 번째
#   재발 실측). pgrep -x 는 comm 15자 잘림에 걸린다. pidof 만 안전하다.
mps_alive(){ pidof nvidia-cuda-mps-control >/dev/null 2>&1; }

do_status(){
  echo "═══ PRISM 상태 ($(date -u +%H:%M:%S) UTC) ═══"
  DS=$(kubectl -n $NSK get ds -l $DS_SEL --no-headers 2>/dev/null | wc -l)
  DSR=$(kubectl -n $NSK get pods -l $DS_SEL --no-headers 2>/dev/null | awk 'BEGIN{n=0} {split($2,a,"/"); if(a[1]!="" && a[1]==a[2]) n++} END{print n}' || true)
  WH=$(kubectl -n $NSK get deploy -l $WH_SEL --no-headers 2>/dev/null | wc -l)
  MW=$(kubectl get mutatingwebhookconfigurations $MWC --no-headers 2>/dev/null | wc -l)
  VP=$(kubectl get validatingadmissionpolicy $VAP --no-headers 2>/dev/null | wc -l)
  LSU=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
  NPU=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/npu-pe}' 2>/dev/null)
  MPS=$(mps_alive && echo 가동 || echo 없음)
  NV=$(kubectl -n $NSK get ds $NV_DS -o jsonpath='{.spec.template.spec.nodeSelector.kubernetes\.io/hostname}' 2>/dev/null)
  echo "  DS(plugin·controller·feedback·advertiser): ${DS}개 (전 컨테이너 Ready 파드: ${DSR})"
  echo "  webhook Deploy: ${WH}  MutatingWebhookCfg: ${MW}  NPU VAP: ${VP}"
  echo "  노출: lsu=${LSU:-없음} npu-pe=${NPU:-없음}  MPS: $MPS"
  echo "  표준 nvidia plugin: nodeSelector=${NV:-없음} ($([ "$NV" = "$NODE" ] && echo 켜짐 || echo 꺼짐))"
  echo "  LSU 계열 파드: $(lsu_pods | wc -l)개"
  UP=$((DS + WH + MW)); MPSN=$(mps_alive && echo 1 || echo 0)
  if [ "$UP" = 3 ] && [ "$MPSN" = 1 ] && [ -n "$LSU" ]; then echo "  ▶ 판정: 올라가 있음"
  elif [ "$UP" = 0 ] && [ "$MPSN" = 0 ]; then echo "  ▶ 판정: 내려가 있음"
  else echo "  ▶ 판정: ★어중간함 — up 또는 down 을 다시 실행하라(멱등)"; fi
}

do_down(){
  local FORCE=0 K8SGPU=0
  for a in "$@"; do
    [ "$a" = "--force" ] && FORCE=1
    [ "$a" = "--k8s-gpu" ] && K8SGPU=1
  done
  say "① LSU 계열 파드 확인"
  PODS=$(lsu_pods)
  if [ -n "$PODS" ]; then
    echo "$PODS" | sed 's/^/    /'
    if [ $FORCE = 1 ]; then
      say "★--force: 위 파드들이 크레딧 동결될 수 있다(Exp_140). 파드는 지우지 않는다 — 계속."
    else
      die "LSU 계열 파드가 있다 — 내리면 armed 파드가 동결된다. 파드 소유자와 정리 후 재실행" \
          "위 파드를 소유자가 정리하면 재실행. 정말 강행하려면 --force"
    fi
  fi
  say "② down 직전 상태 기록 → $STATE/"
  mkdir -p $STATE
  # ★멱등 보존: down 2회차엔 대상이 이미 없어 빈 기록이 된다 — 비어 있지 않을 때만
  #   덮어쓴다(1차 검증에서 2회차 down 이 caBundle 백업을 0B 로 파괴해 up 이
  #   cert 재발급 폴백으로 빠졌다. 동작은 했지만 백업 보존이 맞다).
  kubectl -n $NSK get ds -l $DS_SEL -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)['items']
out={}
if d:
    t=d[0]['spec']['template']['spec']
    out['images']={c['name']:c['image'] for c in t['containers']}
    out['env']={c['name']:{e['name']:e.get('value') for e in c.get('env',[]) if 'value' in e} for c in t['containers']}
    out['args']={c['name']:c.get('args',[]) for c in t['containers']}
import subprocess
out['allocatable']=json.loads(subprocess.run(['kubectl','get','node','$NODE','-o','json'],capture_output=True,text=True).stdout)['status']['allocatable']
print(json.dumps(out,ensure_ascii=False,indent=1))" > $STATE/pre_down.json.tmp
  if [ -s $STATE/pre_down.json.tmp ] && grep -q images $STATE/pre_down.json.tmp; then
    mv $STATE/pre_down.json.tmp $STATE/pre_down.json
  else
    rm -f $STATE/pre_down.json.tmp; say "   (DS 이미 없음 — 기존 기록 보존)"
  fi
  kubectl get mutatingwebhookconfigurations $MWC -o jsonpath='{.webhooks[0].clientConfig.caBundle}' \
    > $STATE/mwc_cabundle.b64.tmp 2>/dev/null
  if [ -s $STATE/mwc_cabundle.b64.tmp ]; then
    mv $STATE/mwc_cabundle.b64.tmp $STATE/mwc_cabundle.b64
    say "   caBundle 백업($(wc -c < $STATE/mwc_cabundle.b64)B)"
  else
    rm -f $STATE/mwc_cabundle.b64.tmp; say "   (MWC 이미 없음 — 기존 caBundle 백업 보존)"
  fi
  say "③ MutatingWebhookConfiguration·VAP 제거 (설정만 남기면 전 파드 생성이 느려진다)"
  kubectl delete mutatingwebhookconfigurations $MWC --ignore-not-found
  kubectl delete validatingadmissionpolicybinding $VAP --ignore-not-found
  kubectl delete validatingadmissionpolicy $VAP --ignore-not-found
  say "④ 워크로드 제거 (라벨 기준)"
  kubectl -n $NSK delete ds -l $DS_SEL --ignore-not-found
  kubectl -n $NSK delete deploy,svc -l $WH_SEL --ignore-not-found
  for i in $(seq 1 30); do
    [ "$(kubectl -n $NSK get pods -l $DS_SEL --no-headers 2>/dev/null | wc -l)" = 0 ] \
      && [ "$(kubectl -n $NSK get pods -l $WH_SEL --no-headers 2>/dev/null | wc -l)" = 0 ] && break
    sleep 2
  done
  say "⑤ MPS 종료 (★systemd 서비스다 — on_off 검증에서 발견: quit 만으로는 Restart 가 되살린다)"
  systemctl stop nvidia-mps 2>/dev/null || true
  sleep 2
  if mps_alive; then
    P=$(pidof nvidia-cuda-mps-control | awk '{print $1}')
    say "   서비스 정지 후에도 잔존 → kill $P"
    kill $P 2>/dev/null; sleep 1
  fi
  mps_alive && die "MPS 데몬이 남아 있다 — 다른 CUDA 프로세스가 조용히 붙는다" || say "   MPS 정지 확인(systemctl stop — enabled 는 유지, up 이 start 한다)"
  CM=$(nvidia-smi -q 2>/dev/null | grep -m1 "Compute Mode" | awk -F: '{gsub(/ /,"",$2);print $2}')
  say "⑥ GPU compute mode = $CM (KRAKEN 은 Default 로 운용 — 변경 이력 없음, 원복 불요)"
  say "⑦ NPU 점유 확인"
  BUSY=$(furiosa-smi ps 2>/dev/null | grep -cE "^\| [0-9]" || true)
  [ "${BUSY:-0}" = 0 ] && say "   NPU 점유 0" || say "   ★NPU 점유 ${BUSY}건 — furiosa-smi ps 로 확인(KRAKEN 프록시는 파드와 함께 종료됨)"
  say "⑧ 소켓 잔재 정리"
  find /var/lib/kraken/socks -name "bless-*.sock" -delete 2>/dev/null
  find /var/lib/kraken/socks -name "trace.*.txt" -delete 2>/dev/null
  say "⑨ 확장 자원 소멸 확인"
  for i in $(seq 1 30); do
    L=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
    [ -z "$L" ] || [ "$L" = "0" ] && break
    sleep 2
  done
  L=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
  N=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/npu-pe}' 2>/dev/null)
  say "   lsu=${L:-소멸} npu-pe=${N:-소멸} (kubelet 은 plugin 소켓 소멸 시 0 으로 내린다 — 항목은 남을 수 있음)"
  if [ $K8SGPU = 1 ]; then
    say "⑩ (--k8s-gpu) 표준 nvidia plugin 켜기 (nodeSelector → $NODE)"
    kubectl -n $NSK patch ds $NV_DS --type=json \
      -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/nodeSelector/kubernetes.io~1hostname\",\"value\":\"$NODE\"}]" \
      && say "   nvidia.com/gpu 광고가 곧 올라온다 (up 이 자동으로 되돌린다)"
  fi
  say "DOWN COMPLETE ($(( $(date +%s)-T0 ))s) — 호스트에서 GPU·NPU 를 바로 쓸 수 있다"
}

do_up(){
  say "① 표준 nvidia plugin 원위치 (켜져 있었다면)"
  NV=$(kubectl -n $NSK get ds $NV_DS -o jsonpath='{.spec.template.spec.nodeSelector.kubernetes\.io/hostname}' 2>/dev/null)
  if [ "$NV" = "$NODE" ]; then
    kubectl -n $NSK patch ds $NV_DS --type=json \
      -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/nodeSelector/kubernetes.io~1hostname\",\"value\":\"$NV_OFF\"}]"
    say "   nvidia plugin 광고 끔(Exp_70 상태로)"
  fi
  say "② MPS 기동 (systemd nvidia-mps.service — Exp_72 영속화, on_off 에서 확인)"
  systemctl start nvidia-mps 2>/dev/null || die "MPS 서비스 기동 실패" "systemctl status nvidia-mps"
  sleep 2
  mps_alive && say "   MPS 가동" || die "MPS 데몬이 안 보인다" "systemctl status nvidia-mps"
  say "③ 정본 매니페스트 적용 (배율 1.35 는 매니페스트 내장 CM — CM 만 따로 패치 금지)"
  kubectl apply -f device-plugin/deploy/daemonset-lsu.yaml >/dev/null || die "DS apply 실패"
  kubectl apply -f device-plugin/deploy/npu-catalog-cm.yaml >/dev/null 2>&1 || true
  kubectl apply -f device-plugin/deploy/npu-fit-policy.yaml >/dev/null || die "VAP apply 실패"
  kubectl apply -f webhook/deploy/webhook.yaml >/dev/null || die "webhook apply 실패"
  say "④ MutatingWebhookConfiguration 복원 (down 때 백업한 caBundle — Secret 과 정합)"
  if [ -s $STATE/mwc_cabundle.b64 ]; then
    python3 - <<'PYEOF' | kubectl apply -f - >/dev/null || die "MWC 복원 실패"
import sys
tpl = open("webhook/deploy/webhook_config.yaml").read()
ca = open("/var/lib/kraken/prism_updown/mwc_cabundle.b64").read().strip()
sys.stdout.write(tpl.replace("<BASE64_CA_BUNDLE>", ca))
PYEOF
  else
    say "   caBundle 백업 없음 → tools/webhook_cert.sh 로 재발급 필요"
    bash tools/webhook_cert.sh || die "인증서 재발급 실패" "tools/webhook_cert.sh 를 수동 확인"
  fi
  say "⑤ 정착 대기 (파드 전 컨테이너 Ready + 노출 240)"
  for i in $(seq 1 60); do
    R=$(kubectl -n $NSK get pods -l $DS_SEL --no-headers 2>/dev/null | awk 'BEGIN{n=0} {split($2,a,"/"); if(a[1]!="" && a[1]==a[2]) n++} END{print n}' || true)
    L=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
    [ "$R" = 1 ] && [ "$L" = "240" ] && break
    sleep 5
  done
  [ "$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}')" = "240" ] \
    || die "노출 240 미복귀" "kubectl -n kube-system logs ds/ketris-device-plugin-lsu -c plugin"
  say "⑥ deploy_verify (배율 네 곳·스위치·so md5)"
  bash tools/deploy_verify.sh >/tmp/prism_up_verify.log 2>&1 \
    || { sleep 30; bash tools/deploy_verify.sh >/tmp/prism_up_verify.log 2>&1; } \
    || die "deploy_verify 실패" "/tmp/prism_up_verify.log 확인"
  say "   deploy_verify 통과"
  say "⑦ 6축 (정착 대기 자동)"
  bash tools/axis_liveness.sh >/tmp/prism_up_axis.log 2>&1
  grep -q "전 축 PASS" /tmp/prism_up_axis.log || die "6축 실패" "/tmp/prism_up_axis.log 확인"
  say "   6축 전 축 PASS"
  say "⑧ down 직전 기록과 대조"
  if [ -s $STATE/pre_down.json ]; then
    python3 - <<'PYEOF'
import json, subprocess
pre = json.load(open("/var/lib/kraken/prism_updown/pre_down.json"))
cur = json.loads(subprocess.run(["kubectl","-n","kube-system","get","ds","-l","app=ketris-device-plugin-lsu","-o","json"],capture_output=True,text=True).stdout)["items"][0]["spec"]["template"]["spec"]
imgs = {c["name"]: c["image"] for c in cur["containers"]}
envs = {c["name"]: {e["name"]: e.get("value") for e in c.get("env",[]) if "value" in e} for c in cur["containers"]}
diff = []
for k, v in pre.get("images",{}).items():
    if imgs.get(k) != v: diff.append(f"이미지 {k}: {v} → {imgs.get(k)}")
for cn, es in pre.get("env",{}).items():
    for en, ev in es.items():
        if envs.get(cn,{}).get(en) != ev: diff.append(f"env {cn}/{en}: {ev} → {envs.get(cn,{}).get(en)}")
print("   대조: " + ("일치 (이미지·스위치 전항)" if not diff else "★차이 — " + "; ".join(diff)))
PYEOF
  else
    say "   pre_down.json 없음 — 대조 생략(첫 up)"
  fi
  say "UP COMPLETE ($(( $(date +%s)-T0 ))s)"
}

case "${1:-}" in
  down)   shift; do_down "$@";;
  up)     shift; do_up "$@";;
  status) do_status;;
  *) echo "usage: tools/prism.sh {down [--force] [--k8s-gpu] | up | status}"; exit 2;;
esac
