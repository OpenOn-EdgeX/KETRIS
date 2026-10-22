#!/bin/bash
# [Exp_123 2부] 배포 검증 — **도는 파드가 의도한 소스인지** 대조한다.
#
# usage: tools/deploy_verify.sh [expected_stamp]
#   인자가 없으면 현재 작업트리에서 스탬프를 계산해 그것을 기대값으로 쓴다.
#
# 검사 3단:
#   ① DS 스펙의 이미지 태그 == 매니페스트의 태그   (Exp_122: apply 가 태그를 되돌림)
#   ② 도는 파드 두 컨테이너의 build= 스탬프 == 기대값  (Exp_121: 같은 태그·구 내용)
#   ③ 스탬프가 unstamped 가 아닐 것            (스탬프 없이 빌드된 이미지 차단)
# ★어느 하나라도 어긋나면 실패한다. "아마 맞겠지" 로 넘어가지 않는다.
set -u
cd "$(dirname "$0")/.."
NSK=kube-system; DS=ketris-device-plugin-lsu
WANT="${1:-$(./tools/build_stamp.sh)}"
FAIL=0
say(){ echo "$@"; }
bad(){ echo "  ❌ $*"; FAIL=1; }

# ★비교는 **내용 해시**로 한다. git sha 는 사람이 읽기 위한 것이고 이미지 내용을
#   결정하지 않는다 — 내용 변화 없는 커밋만으로 검증이 깨지면 도구가 자기를 무효화한다
#   (Exp_123 에서 매니페스트를 해시 입력에 넣었다가 같은 형태로 헛돌았다).
WANT_H="${WANT##*-}"
say "기대 스탬프: $WANT  (비교 대상 내용 해시: $WANT_H)"

# ① 태그 일치
MTAG=$(grep -m1 -oE 'image: ketris-device-plugin:[A-Za-z0-9._-]+' \
        device-plugin/deploy/daemonset-lsu.yaml | cut -d: -f3)
for c in plugin controller; do
  DTAG=$(kubectl -n $NSK get ds $DS \
         -o jsonpath="{.spec.template.spec.containers[?(@.name=='$c')].image}" 2>/dev/null | cut -d: -f2)
  if [ "$DTAG" != "$MTAG" ]; then
    bad "① $c 이미지 태그 불일치: DS=$DTAG 매니페스트=$MTAG (apply 가 되돌렸을 수 있다)"
  else
    say "  ✅ ① $c 태그 $DTAG"
  fi
done

# ② 스탬프 일치 (도는 파드의 로그에서 읽는다 — 이미지가 아니라 실행체가 근거다)
PODS=$(kubectl -n $NSK get pod -l app=$DS -o name 2>/dev/null)
[ -z "$PODS" ] && bad "② 파드를 찾지 못했다"
for p in $PODS; do
  for c in plugin controller; do
    # [Exp_131] 롤아웃 직후엔 컨테이너가 막 떠서 build= 줄이 아직 없을 수 있다.
    #   재시도 없이 읽으면 **정상 배포를 실패로 오판한다**(Exp_131 에서 1회 발생).
    GOT=""
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      GOT=$(kubectl -n $NSK logs "$p" -c $c 2>/dev/null | grep -m1 -oE 'build=[A-Za-z0-9.+_-]+' | cut -d= -f2)
      [ -n "$GOT" ] && break
      sleep 2
    done
    # [Exp_134] 로그 회전 폴백 — kubelet 로그는 회전하므로 오래 도는 파드의
    #   build= 줄은 사라진다(Exp_134 0부 실측: 기동 2.5시간 뒤 소실).
    #   실행체가 기동 시 남긴 스탬프 파일을 쓰되, **파드 기동 시각 이후에
    #   쓰인 것만** 인정한다(구 파드의 잔재로 오판 방지).
    if [ -z "$GOT" ]; then
      F=/var/lib/kraken/stamp-$c
      PST=$(kubectl -n $NSK get "$p" -o jsonpath='{.status.startTime}' 2>/dev/null)
      if [ -s "$F" ] && [ -n "$PST" ]; then
        FT=$(stat -c %Y "$F" 2>/dev/null || echo 0)
        PT=$(date -d "$PST" +%s 2>/dev/null || echo 0)
        if [ "$FT" -ge "$PT" ]; then
          GOT=$(head -1 "$F")
          say "  ⓘ ${p#pod/}/$c: 로그에 build= 가 없다(회전) — 스탬프 파일로 검증"
        else
          bad "② ${p#pod/}/$c: 스탬프 파일이 파드 기동보다 오래됐다(구 파드 잔재)"
        fi
      fi
    fi
    if [ -z "$GOT" ]; then
      bad "② ${p#pod/}/$c: build= 줄이 없다 (구 이미지이거나 스탬프 미구현)"
    elif [ "$GOT" = "unstamped" ]; then
      bad "③ ${p#pod/}/$c: unstamped — BUILD_STAMP 없이 빌드된 이미지다"
    elif [ "${GOT##*-}" != "$WANT_H" ]; then
      bad "② ${p#pod/}/$c 내용 불일치: 도는것=$GOT 기대=$WANT"
    else
      [ "$GOT" = "$WANT" ] || say "  ⓘ ${p#pod/}/$c 커밋 표기만 다름(내용 동일): $GOT"
      say "  ✅ ② ${p#pod/}/$c $GOT"
    fi
  done
done

# ═══ [Exp_143 1-A] 라이브 드리프트 검사 ═══════════════════════════════════
# 스탬프는 "도는 코드가 의도한 코드인가"만 본다. 여기서부터는 **라이브 상태가
# 선언(정본·매니페스트)과 같은가**를 본다 — Exp_142에서 apply 가 CM 배율을
# 1.0 으로 되돌린 채 시험이 진행됐고(무경보, FailedScheduling 으로 뒤늦게 발견),
# 그 상태로 공인시험을 보면 활용률이 62%대로 떨어진다.
# ★기대값을 상수로 박지 않는다 — 배율·노출은 정본 카탈로그에서, 스위치는
#   매니페스트에서 유도한다.
say "── 드리프트 검사 (Exp_143) ──"
MANIFEST=device-plugin/deploy/daemonset-lsu.yaml
NODE=gpu-npu-server-02

# ④ 오버커밋 배율 — 네 곳 일치 (정본 / 매니페스트 CM / 라이브 CM / allocatable)
#   등급 = 실패. 배율은 공인시험 성립 조건이라 "의도적 차이"가 없다.
F_SRC=$(python3 -c "import json;print(json.load(open('lsu/catalog.json'))['overcommit_factor'])")
F_MAN=$(python3 - "$MANIFEST" <<'PYEOF'
import sys, yaml, json
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "ketris-lsu-catalog":
        print(json.loads(d["data"]["catalog.json"])["overcommit_factor"]); break
PYEOF
)
F_LIVE=$(kubectl get cm ketris-lsu-catalog -n $NSK -o jsonpath='{.data.catalog\.json}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['overcommit_factor'])" 2>/dev/null)
PHYS=$(python3 -c "import json;c=json.load(open('lsu/catalog.json'));print(max(int(d['lsu']) for d in c['devices'].values() if d.get('measured')))")
EXP_LSU=$(python3 -c "print(int($PHYS*$F_SRC+0.5))")   # plugin.go:170 과 같은 half-up
LSU=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/lsu}' 2>/dev/null)
if python3 -c "import sys;sys.exit(0 if float('$F_SRC')==float('$F_MAN')==float('${F_LIVE:-nan}') else 1)" 2>/dev/null; then
  say "  ✅ ④ 배율 일치: 정본=매니페스트=라이브 CM = $F_SRC"
else
  bad "④ 배율 불일치: 정본=$F_SRC 매니페스트=$F_MAN 라이브 CM=${F_LIVE:-조회실패} — setfactor.sh 만 쓰고 정본을 안 고쳤거나, apply 가 CM 을 되돌렸다(Exp_142)"
fi
if [ "${LSU:-0}" = "$EXP_LSU" ]; then
  say "  ✅ ④ 노출 lsu=$LSU (= $PHYS×$F_SRC half-up)"
else
  bad "④ 노출 불일치: allocatable lsu=${LSU:-없음}, 정본 기대=$EXP_LSU — 롤아웃 누락 또는 배율 드리프트"
fi

# ⑤ npu-pe — 광고는 실점유 반영 동적(Exp_122, 30초 예약 창 포함)이라 정본 기대값이
#   없다. 0 이면 경고(플러그인 광고 자체가 죽었을 가능성), 그 외는 표시만.
NPU=$(kubectl get node $NODE -o jsonpath='{.status.allocatable.keti\.re\.kr/npu-pe}' 2>/dev/null)
if [ "${NPU:-0}" = "0" ] || [ -z "$NPU" ]; then
  say "  ⚠ ⑤ npu-pe=${NPU:-없음} — 동적 광고이나 0 은 광고 소멸 의심(30초 예약 창이면 재확인)"
else
  say "  ✅ ⑤ npu-pe=$NPU (동적 — 8 미만이면 점유·예약 중)"
fi

# ⑥ 스위치 — 매니페스트 선언 vs 라이브 DS. 등급 = 경고.
#   사유: 실험 중 kubectl set env 로 의도적으로 바꾸는 운용이 실재한다
#   (Exp_140 tracer, Exp_142 REDIST). 시험 성립을 직접 깨는 것은 배율·노출이므로
#   그 둘만 실패, 스위치는 눈에 띄는 경고로 둔다. 코드 기본값도 함께 표기 —
#   매니페스트가 코드 기본과 다른 것은 알려진 의도(KRAKEN_CTLCHECK, Exp_137)다.
WARN=0
python3 - "$MANIFEST" > /tmp/dv_switch_man.txt <<'PYEOF'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "DaemonSet":
        for c in d["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []):
                if "value" in e:
                    print(c["name"], e["name"], e["value"])
            print(c["name"], "__FAST_TEARDOWN__",
                  "on" if any("--fast-teardown-dir" in a for a in c.get("args", [])) else "off")
PYEOF
kubectl -n $NSK get ds $DS -o json > /tmp/dv_ds_live.json 2>/dev/null
python3 - /tmp/dv_ds_live.json > /tmp/dv_switch_live.txt <<'PYEOF'
import sys, json
d = json.load(open(sys.argv[1]))
for c in d["spec"]["template"]["spec"]["containers"]:
    for e in c.get("env", []):
        if "value" in e:
            print(c["name"], e["name"], e["value"])
    print(c["name"], "__FAST_TEARDOWN__",
          "on" if any("--fast-teardown-dir" in a for a in c.get("args", [])) else "off")
PYEOF
for KEY in KRAKEN_REDIST KRAKEN_BOOSTER KRAKEN_CTLCHECK KRAKEN_TRACER KRAKEN_SCANNER_LINK KRAKEN_FAST_TEARDOWN KRAKEN_FEEDBACK KRAKEN_ADVERTISER KRAKEN_TIME_WC __FAST_TEARDOWN__; do
  for CN in plugin controller feedback advertiser; do
    M=$(awk -v c=$CN -v k=$KEY '$1==c && $2==k {print $3}' /tmp/dv_switch_man.txt)
    L=$(awk -v c=$CN -v k=$KEY '$1==c && $2==k {print $3}' /tmp/dv_switch_live.txt)
    [ -z "$M" ] && [ -z "$L" ] && continue
    if [ "$M" = "$L" ]; then
      say "  ✅ ⑥ $CN/$KEY: $L (매니페스트와 일치)"
    else
      say "  ⚠ ⑥ $CN/$KEY 다름: 라이브=${L:-미설정} 매니페스트=${M:-미설정} — 실험 중이면 의도 확인, 아니면 apply 로 정합"
      WARN=1
    fi
  done
done
# 코드 기본값 대조(정보) — 소스에서 추출한다(상수 금지 규칙).
CD_REDIST=$(grep -oE 'KRAKEN_REDIST", *"[0-9]"' controller/feeder/feeder.py | grep -oE '"[0-9]"$' | tr -d '"')
CD_CTL=$(grep -oE 'KRAKEN_CTLCHECK", *"[0-9]"' controller/feeder/feeder.py | grep -oE '"[0-9]"$' | tr -d '"')
say "  ⓘ 코드 기본값: REDIST=${CD_REDIST:-?} CTLCHECK=${CD_CTL:-?} (BOOSTER·TRACER·SCANNER_LINK 는 =='1' 비교 구조라 기본 꺼짐) — 매니페스트가 코드 기본과 다른 건 CTLCHECK=1 뿐(Exp_137 의도)"

# ⑦ libbless 배치본 — 레포의 이미지 입력본과 md5 일치 (entrypoint place 대상 3종)
for so in libbless.so gating_lib.so kernel_tracer.so; do
  R=$(md5sum device-plugin/image/lib/$so 2>/dev/null | cut -d' ' -f1)
  H=$(md5sum /var/lib/kraken/lib/$so 2>/dev/null | cut -d' ' -f1)
  if [ -n "$R" ] && [ "$R" = "$H" ]; then
    say "  ✅ ⑦ $so 배치본 = 레포 입력본 ($H)"
  else
    bad "⑦ $so 배치본($H) ≠ 레포 입력본($R) — entrypoint 배치 실패 또는 수동 교체 잔재"
  fi
done

# ⑧ [Exp_159] webhook 인증서 만료·실호출 감시 — 만료되면 failurePolicy Ignore 라
#   파드 생성은 되고 제어만 조용히 빠진다(경보 없는 뚫림). 두 검사:
#   (a) 만료 잔여일 표시 + 30일 미만 경고. 30일 근거 = 이 검증의 실행 빈도가 Exp 단위
#       (주 2~3회)이고, 과제 일정상 관측된 최장 무접촉 구간이 ~3주(Exp_92 전후 실측)
#       — 그 구간을 덮고 재발급 작업(webhook_cert.sh, 1일 내)을 시작할 여유.
#   (b) 실호출 프로브 — 무해 파드 1개 생성·삭제로 /healthz reviews 가 실제로 늘어야
#       한다. 0 이면 "webhook 이 안 불리는 상태"(만료·미배선·MWC 소실)를 그 자리에서
#       잡는다(Exp_156 음성 대조 원칙 — 존재 검사가 아니라 동작 검사).
CRT_END=$(kubectl -n $NSK get secret ketris-webhook-tls -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$CRT_END" ]; then
  DAYS=$(( ( $(date -d "$CRT_END" +%s) - $(date +%s) ) / 86400 ))
  if [ "$DAYS" -lt 30 ]; then
    say "  ⚠ ⑧ webhook 인증서 만료 ${DAYS}일 전 ($CRT_END) — webhook_cert.sh 재발급 필요"
    WARN=1
  else
    say "  ✅ ⑧ webhook 인증서 만료까지 ${DAYS}일 ($CRT_END)"
  fi
else
  bad "⑧ webhook 인증서(tls.crt)를 읽지 못했다 — Secret ketris-webhook-tls 확인"
fi
# ★reviews 는 webhook **파드별** 카운터(레플리카 2, Service 분산) — Service IP 로
#   두 번 읽으면 다른 파드가 답해 delta 가 무의미하다(Exp_159 실측: 404→392).
#   엔드포인트 파드 IP 각각을 합산해 비교한다.
wh_reviews_sum(){
  local sum=0 v ip
  for ip in $(kubectl -n $NSK get endpointslices -l kubernetes.io/service-name=ketris-webhook \
      -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null); do
    v=$(curl -sk --max-time 3 https://$ip:8443/healthz 2>/dev/null | grep -oP '"reviews": \K[0-9]+')
    [ -n "$v" ] && sum=$((sum + v))
  done
  echo $sum
}
RV0=$(wh_reviews_sum)
kubectl -n default run dv-probe --image=ketris-device-plugin:$MTAG --restart=Never \
  --command -- sleep 5 >/dev/null 2>&1
sleep 3
RV1=$(wh_reviews_sum)
kubectl -n default delete pod dv-probe --now >/dev/null 2>&1
if [ -n "$RV0" ] && [ -n "$RV1" ] && [ "$RV1" -gt "$RV0" ]; then
  say "  ✅ ⑧ webhook 실호출 확인 (reviews $RV0→$RV1 — 프로브 파드가 지나감)"
else
  say "  ⚠ ⑧ webhook 이 호출되지 않는다 (reviews ${RV0:-?}→${RV1:-?}) — 만료·MWC·배선 확인 (Ignore 라 조용히 뚫린 상태)"
  WARN=1
fi

# ⑨ [Exp_161] dirty 빌드 스탬프 경고 — +dirty 이미지는 태그만으로 재현 불가라
#   시험 스택이 될 수 없다(빌드 거부 장치는 tools/build_image.sh).
LIVE_STAMP=$(head -1 /var/lib/kraken/stamp-plugin 2>/dev/null)
case "$LIVE_STAMP" in
  *+dirty*) say "  ⚠ ⑨ 라이브 스탬프가 dirty 빌드($LIVE_STAMP) — 태그 재현 불가, 시험 스택 부적격"; WARN=1 ;;
  "") : ;;
  *) say "  ✅ ⑨ 라이브 스탬프 클린 빌드 ($LIVE_STAMP)" ;;
esac

[ $FAIL = 0 ] && { [ "${WARN:-0}" = 1 ] && say "배포 검증 통과 (⚠ 경고 있음 — 위 ⑥/⑧/⑨)" || say "배포 검증 통과"; exit 0; }
say "배포 검증 실패 — 롤아웃을 신뢰하지 말 것"
exit 1
