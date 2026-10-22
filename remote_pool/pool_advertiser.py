#!/usr/bin/env python3
"""[Exp_91→94] KETI 원격 풀 광고·lease·회수 컨트롤러 (본체, CRD 기준 정본).

   RemotePool CR → 232 node 확장리소스 광고 + lease 발급(진실 원천) + 반납 감지 + ★회수 핸드셰이크.
   ★lease = 점유의 진실 원천(소비 노드에 원격 디바이스가 물리적으로 없으므로 kubelet 카운트 불신).
   반납 = podresources 대신 pod liveness 폴링(Turnaround Booster 계열, booster._poll_loop 재사용 패턴).

   ── [Exp_94] 회수 핸드셰이크 (표준 K8s spec=desired / status=observed) ─────────────
   provider(하스퍼 자리)가 쓰는 것 : spec.reclaim, spec.reclaimGracePeriodSeconds
   KETI 컨트롤러가 쓰는 것        : status.phase, conditions, observedGeneration, 시각

       Allocatable ──(spec.reclaim=true)──▶ ReclaimPending ──(leased==0)──▶ Reclaimed
                                                │
                      진입 즉시 광고를 leased 로 축소(여유분 회수 = 신규 유입 차단),
                      lease 가 하나씩 빠질 때마다 광고를 더 줄인다.
                      유예 초과 시 lease 보유 파드를 강제 종료(--force).

   유예 정책 하나로 두 분기를 덮는다:
     reclaimGracePeriodSeconds > 0 → 예고형(드레인 후 강제)
     reclaimGracePeriodSeconds = 0 → 즉시형(대기 없이 강제)
   → 하스퍼의 "회수 예고 가능?" 답을 기다리지 않고 구현을 완비할 수 있다(A-1 §확인필요 2).

   ── 안전장치 (capability_watcher 의 규약 계승) ────────────────────────────────
   · 광고 **축소만 자동**. spec.compute.units 가 늘어도 자동 확대하지 않고 로그만 남긴다.
   · CR 조회 실패·손상 → 현 광고 유지(0 으로 만들지 않는다).
   · Reclaimed 도달 후에는 spec.reclaim 이 내려가야 다시 Allocatable 로 복귀한다(플래핑 방지).

   env: POOL(rtx5090-233), NODE(gpu-npu-server-02), RES(keti.re.kr/remote-lsu), INTERVAL(0.5),
        LOG(/tmp/kraken_lease.log) — 이벤트 ns 는 default 고정(Exp_151)
"""
import json, os, subprocess, sys, time

POOL = os.environ.get("POOL", "rtx5090-233")
NODE = os.environ.get("NODE", "gpu-npu-server-02")
RES = os.environ.get("RES", "keti.re.kr/remote-lsu")
INTERVAL = float(os.environ.get("INTERVAL", "0.5"))
# [Exp_152 0-C] EVENT_NS 잔재 제거 — 이벤트는 default 고정(Exp_151 수정). 죽은 ns 참조였다
LOG = os.environ.get("LOG", "/tmp/kraken_lease.log")
RESP = RES.replace("/", "~1")

PH_ALLOC, PH_PENDING, PH_DONE = "Allocatable", "ReclaimPending", "Reclaimed"

# ── [Exp_153 2부] 전송 계층 — in-cluster API 직호출 ─────────────────────────
#   kubectl 서브프로세스는 호출당 fork+exec 로 **대기 모드에서만 CPU 26%** 를
#   먹었다(0.5s 폴링 실측). 사이드카(SA 토큰)에서는 표준 라이브러리 HTTP 로
#   직호출한다 — controller 의 _remote_pools(Exp_140)와 같은 패턴.
#   SA 가 없으면(호스트 수동 실행) kubectl 로 폴백해 기존 사용법을 보존한다.
import ssl as _ssl
import urllib.request as _ur
import urllib.error as _ue

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_INCLUSTER = bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and os.path.exists(_SA + "/token")
if _INCLUSTER:
    _API = "https://%s:%s" % (os.environ["KUBERNETES_SERVICE_HOST"],
                              os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
    _CTX = _ssl.create_default_context(cafile=_SA + "/ca.crt")


def _tok():
    return open(_SA + "/token").read().strip()


def kapi(method, path, body=None, content_type="application/json"):
    """(status:int, json|None). 4xx/5xx 도 status 로 돌려준다(예외 아님)."""
    req = _ur.Request(_API + path, method=method,
                      data=(json.dumps(body).encode() if body is not None else None),
                      headers={"Authorization": "Bearer " + _tok(),
                               "Content-Type": content_type})
    try:
        with _ur.urlopen(req, timeout=10, context=_CTX) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except _ue.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None                    # 네트워크 계열 — 일시 실패로 취급



def kc(args, inp=None):
    return subprocess.run(["kubectl"] + args, input=inp, capture_output=True, text=True, timeout=15)


def now_rfc3339():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(*a):
    line = "\t".join(str(x) for x in a)
    try:
        open(LOG, "a").write(line + "\n")
    except Exception:
        pass
    print("[pool] " + line, flush=True)


def emit(reason, msg, warn=False):
    """A-4 규격 이벤트 — 회수 진행을 표준 K8s Event 로 낸다.

    [Exp_151 1부] ★두 결함 수정 — 이 경로는 **한 번도 발행에 성공한 적이 없었다**:
      ① EVENT_NS 기본값(kraken-demo)이 현 클러스터에 없음(Exp_86/92 재생성 때
        소멸) → NotFound 전건 실패. Node 이벤트는 "default" 에 낸다(Exp_150 규칙).
      ② involvedObject 에 namespace 를 넣었었다 — Node 는 클러스터 스코프라
        namespace 가 없어야 한다(mode_event.py 422 와 같은 계열).
    실패를 stderr 로 드러낸다(T-5 — 조용히 버리면 '발행 코드 있음'이 '발행됨'으로 읽힌다).
    """
    ev = {"apiVersion": "v1", "kind": "Event",
          "metadata": {"generateName": "kraken-remotepool-", "namespace": "default"},
          "type": "Warning" if warn else "Normal", "reason": reason, "message": msg,
          "involvedObject": {"kind": "Node", "name": NODE},
          "source": {"component": "kraken-pool-advertiser"}, "action": reason}
    if _INCLUSTER:
        st, _ = kapi("POST", "/api/v1/namespaces/default/events", ev)
        if st not in (200, 201):
            print("[pool][경고] 이벤트 발행 실패 reason=%s: HTTP %s" % (reason, st), flush=True)
        return
    r = subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(ev),
                       text=True, capture_output=True)
    if r.returncode != 0:
        print("[pool][경고] 이벤트 발행 실패 reason=%s: %s"
              % (reason, (r.stderr or "").strip()[:160]), flush=True)


def get_pool():
    """반환: (pool|None, notfound:bool). [Exp_153 2부] CR '진짜 삭제'(NotFound)와
    일시 조회 실패를 가른다 — 전자는 광고를 거두고 대기로, 후자는 현 광고 유지."""
    if _INCLUSTER:
        st, body = kapi("GET", f"/apis/keti.re.kr/v1alpha1/remotepools/{POOL}")
        if st == 200 and body:
            return body, False
        return None, (st == 404)
    r = kc(["get", "remotepool", POOL, "-o", "json"])
    if r.returncode != 0:
        return None, ("NotFound" in (r.stderr or ""))
    try:
        return json.loads(r.stdout), False
    except Exception:
        return None, False               # 손상 → 현 광고 유지


def advertise(units):
    for sub in ("capacity", "allocatable"):
        patch = [{"op": "add", "path": f"/status/{sub}/{RESP}", "value": str(units)}]
        if _INCLUSTER:
            kapi("PATCH", f"/api/v1/nodes/{NODE}/status", patch,
                 content_type="application/json-patch+json")
        else:
            kc(["patch", "node", NODE, "--subresource=status", "--type=json",
                "-p", json.dumps(patch)])


def unadvertise():
    for sub in ("capacity", "allocatable"):
        patch = [{"op": "remove", "path": f"/status/{sub}/{RESP}"}]
        if _INCLUSTER:
            kapi("PATCH", f"/api/v1/nodes/{NODE}/status", patch,
                 content_type="application/json-patch+json")
        else:
            kc(["patch", "node", NODE, "--subresource=status", "--type=json",
                "-p", json.dumps(patch)])


def remote_pods():
    """RES 를 요청한 Running 파드 → {podUid: (name, ns, lsu)}."""
    out = {}
    if _INCLUSTER:
        st, body = kapi("GET", "/api/v1/pods?limit=500")
        if st != 200 or body is None:
            return None                  # 조회 실패 → 현 상태 유지(반납 오판 방지)
        items = body.get("items", [])
    else:
        r = kc(["get", "pods", "-A", "-o", "json"])
        if r.returncode != 0:
            return None
        try:
            items = json.loads(r.stdout).get("items", [])
        except Exception:
            return None
    for p in items:
        if p.get("status", {}).get("phase") != "Running":
            continue
        req = 0
        for c in p["spec"]["containers"]:
            v = c.get("resources", {}).get("limits", {}).get(RES)
            if v:
                req += int(v)
        if req > 0:
            out[p["metadata"]["uid"]] = (p["metadata"]["name"],
                                         p["metadata"].get("namespace", "default"), req)
    return out


def force_drain(leases):
    """유예 초과 → lease 보유 파드 강제 종료. 즉시 회수(grace=0) 분기도 이 경로."""
    for l in leases:
        ns = l.get("podNamespace", "default")
        if _INCLUSTER:
            kapi("DELETE", f"/api/v1/namespaces/{ns}/pods/{l['tenant']}"
                 "?gracePeriodSeconds=0")
        else:
            kc(["delete", "pod", l["tenant"], "-n", ns,
                "--force", "--grace-period=0"])
        log(f"{time.time():.2f}", "FORCE_DRAIN", l["tenant"], f"ns={l.get('podNamespace')}")


def cond(t, st, reason, msg):
    return {"type": t, "status": st, "reason": reason, "message": msg,
            "lastTransitionTime": now_rfc3339()}


def patch_status(phase, units, leases, generation, conds, started=None, completed=None):
    st = {"phase": phase, "observedGeneration": generation,
          "advertisedLsu": units, "leasedLsu": sum(l["lsu"] for l in leases),
          "leases": leases, "conditions": conds}
    if started:
        st["reclaimStartedAt"] = started
    if completed:
        st["reclaimCompletedAt"] = completed
    if _INCLUSTER:
        kapi("PATCH", f"/apis/keti.re.kr/v1alpha1/remotepools/{POOL}/status",
             {"status": st}, content_type="application/merge-patch+json")
    else:
        kc(["patch", "remotepool", POOL, "--subresource=status", "--type=merge",
            "-p", json.dumps({"status": st})])


def main():
    """[Exp_153 2부] CR 대기 루프 — "CR 만 올리면 붙는다"를 성립시킨다.
    CR 이 없으면 종료하지 않고 INTERVAL(기존 폴링 상수 재사용)로 기다린다.
    CR 이 생기면 serve() 로 광고·리스·회수를 돌리고, CR 이 삭제되면(NotFound)
    광고를 거두고 다시 대기로 돌아온다. 상주(사이드카) 전제의 구조다."""
    announced = False
    while True:
        pool, notfound = get_pool()
        if pool is None:
            if not announced:
                log(f"# WAIT pool={POOL} — CR 대기 중(주기 {INTERVAL}s, 상주 모드)")
                announced = True
            time.sleep(INTERVAL)
            continue
        announced = False
        serve(pool)                      # CR 삭제(NotFound) 시 리턴 → 다시 대기
        time.sleep(INTERVAL)


def serve(pool):
    spec_units = pool["spec"]["compute"]["units"]
    advertised = spec_units
    advertise(advertised)
    phase = PH_ALLOC
    started = completed = None
    pending_since = None
    leases = {}                # podUid -> lease dict
    log(f"# START pool={POOL} units={spec_units} res={RES} (CRD 기준 회수 핸드셰이크 활성)")

    while True:
        time.sleep(INTERVAL)
        pool, notfound = get_pool()
        if pool is None:
            if notfound:                   # [Exp_153] CR 진짜 삭제 → 광고 거두고 대기로
                unadvertise()
                log(f"{time.time():.2f}", "CR_DELETED", "광고 제거 → CR 대기로 복귀")
                return
            log(f"{time.time():.2f}", "CR_READ_FAIL", "현 광고 유지(일시 실패)")
            continue                       # 안전장치: 광고 건드리지 않음
        gen = pool["metadata"].get("generation", 0)
        want_reclaim = bool(pool["spec"].get("reclaim", False))
        grace = int(pool["spec"].get("reclaimGracePeriodSeconds", 60))
        new_units = pool["spec"]["compute"]["units"]
        if new_units > spec_units:
            log(f"{time.time():.2f}", "UNITS_GROW_IGNORED",
                f"{spec_units}→{new_units} (확대는 자동 안 함 — 실측 후 수동)")
        spec_units = new_units

        pods = remote_pods()
        if pods is None:
            log(f"{time.time():.2f}", "POD_LIST_FAIL", "반납 판정 보류")
            continue                       # 안전장치: 조회 실패를 반납으로 오판하지 않음

        # ── lease 발급/반납 (모든 phase 공통 관측) ──
        for uid, (name, ns, lsu) in pods.items():
            if uid not in leases:
                if phase != PH_ALLOC:
                    continue               # 드레인 중에는 신규 lease 발급 안 함
                leases[uid] = {"leaseId": f"lease-{uid[:8]}", "tenant": name, "lsu": lsu,
                               "podUid": uid, "podNamespace": ns, "issuedAt": now_rfc3339()}
                log(f"{time.time():.2f}", "LEASE_ISSUE", name,
                    f"lsu={lsu}", f"leased={sum(l['lsu'] for l in leases.values())}")
        for uid in list(leases):
            if uid not in pods:
                nm = leases.pop(uid)["tenant"]
                log(f"{time.time():.2f}", "LEASE_RETURN", nm,
                    f"반납 감지(폴링 {INTERVAL}s), leased={sum(l['lsu'] for l in leases.values())}")

        lv = list(leases.values())
        leased = sum(l["lsu"] for l in lv)

        # ── 상태 전이 ──────────────────────────────────────────────
        if phase == PH_ALLOC:
            if want_reclaim:
                phase = PH_PENDING
                started = now_rfc3339()
                pending_since = time.time()
                advertised = leased        # ★여유분 즉시 회수 = 신규 유입 차단
                if advertised > 0:
                    advertise(advertised)
                else:
                    unadvertise()
                log(f"{time.time():.2f}", "RECLAIM_START",
                    f"grace={grace}s leased={leased} → 광고 {advertised} 로 축소(여유분 회수)")
                emit("ReclaimStarted",
                     f"{POOL} 회수 예고 감지 — 여유분 광고 축소({spec_units}→{advertised}), "
                     f"기존 lease {len(lv)}건 드레인 (grace={grace}s)", warn=True)
            else:
                if advertised != spec_units:
                    advertised = spec_units
                    advertise(advertised)

        elif phase == PH_PENDING:
            if not want_reclaim:           # provider 가 회수를 취소
                phase = PH_ALLOC
                advertised = spec_units
                advertise(advertised)
                started = pending_since = None
                log(f"{time.time():.2f}", "RECLAIM_CANCEL", "광고 복원")
                emit("ReclaimCancelled", f"{POOL} 회수 취소 — 광고 {advertised} 복원")
            else:
                if advertised != leased:   # lease 반납분만큼 광고 추가 축소
                    advertised = leased
                    if advertised > 0:
                        advertise(advertised)
                    else:
                        unadvertise()
                    log(f"{time.time():.2f}", "DRAIN_SHRINK", f"광고 → {advertised}")
                if leased == 0:
                    phase = PH_DONE
                    completed = now_rfc3339()
                    unadvertise()
                    dur = time.time() - pending_since if pending_since else 0
                    log(f"{time.time():.2f}", "RECLAIM_DONE", f"드레인 완료 {dur:.2f}s → 광고 해제")
                    emit("ReclaimReady",
                         f"{POOL} 슬라이스 비움 완료 — 광고 해제, 회수 가능 (드레인 {dur:.2f}s)")
                elif pending_since is not None and (time.time() - pending_since) >= grace:
                    log(f"{time.time():.2f}", "GRACE_EXPIRED",
                        f"{grace}s 초과 — lease {len(lv)}건 강제 종료")
                    emit("ReclaimForced",
                         f"{POOL} 유예 {grace}s 초과 — lease {len(lv)}건 강제 종료", warn=True)
                    force_drain(lv)

        elif phase == PH_DONE:
            if not want_reclaim:           # provider 가 되돌림 → 재광고
                phase = PH_ALLOC
                advertised = spec_units
                advertise(advertised)
                started = completed = pending_since = None
                log(f"{time.time():.2f}", "REJOIN", f"회수 해제 — 광고 {advertised} 재개")
                emit("PoolReadvertised", f"{POOL} 회수 해제 — 광고 {advertised} 재개")

        conds = [
            cond("Advertising", "True" if phase == PH_ALLOC else "False", phase,
                 f"advertised={advertised}"),
            cond("Draining", "True" if phase == PH_PENDING else "False", phase,
                 f"leased={leased}"),
            cond("Reclaimed", "True" if phase == PH_DONE else "False", phase,
                 "광고 해제됨" if phase == PH_DONE else "회수 전"),
        ]
        patch_status(phase, advertised, lv, gen, conds, started, completed)


if __name__ == "__main__":
    main()
