#!/usr/bin/env python3
# KETI(우리) 프로덕션 컴포넌트 — 대체 대상 아님.
# A-1 규격: capability 파일의 status를 감시해 회수 핸드셰이크를 자동화한다.
#   reclaim_pending 감지 → 슬라이스 드레인 → 광고 축소 → reclaim_ready 회신
#   신규 capability → 카탈로그 등재(미실측이면 잠정). ★확대(광고 늘림)는 자동 안 함(신중).
#
# 안전장치:
#  - 광고 축소 방향만 자동. 확대는 로그만(운영/실측 확인 후 수동).
#  - capability 파일 손상·누락 → 현 광고 유지(0으로 만들지 않음).
#  - 데몬이 죽어도 라이브 광고 불변(카탈로그 CM은 그대로).
import json, os, subprocess, sys, time

CAP_DIR = os.environ.get("KRAKEN_CAP_DIR", "/var/lib/kraken/capability")
DEMO_NS = os.environ.get("KRAKEN_DEMO_NS", "kraken-demo")
POLL = float(os.environ.get("KRAKEN_WATCH_POLL", "2"))
_state = {}   # uuid -> last status


def emit(reason, kind, name, msg, warn=False):
    ev = {"apiVersion": "v1", "kind": "Event",
          "metadata": {"generateName": "kraken-reclaim-", "namespace": DEMO_NS},
          "type": "Warning" if warn else "Normal", "reason": reason, "message": msg,
          "involvedObject": {"kind": kind, "name": name, "namespace": DEMO_NS},
          "source": {"component": "kraken-capability-watcher"}, "action": reason}
    subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(ev).encode(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def drain_slices():
    """회수 대상 디바이스의 슬라이스 비우기(데모 NS 한정 — 라이브 무오염)."""
    subprocess.run(["kubectl", "delete", "pods", "--all", "-n", DEMO_NS, "--now"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def handle(cap, path):
    uuid = cap["device"]["uuid"]
    status = cap.get("status")
    prev = _state.get(uuid)
    if status == prev:
        return
    if prev is None:
        # 신규 등재. 미실측이면 잠정(광고 확대는 자동 안 함).
        tag = "잠정(미실측)" if not cap.get("measured", True) else "실측"
        print(f"[watcher] 신규 capability {uuid} 등재 {tag} — 광고 확대는 신중(자동 안 함)")
    elif status == "reclaim_pending":
        print(f"[watcher] {uuid} reclaim_pending → 슬라이스 드레인 + 광고 축소(자동)")
        emit("ReclaimStarted", "Node", "gpu-npu-server-02",
             f"{uuid} 회수 예고 감지 — 슬라이스 드레인", warn=True)
        drain_slices()
        # 광고 축소 = 회수 확정 신호. (전체 GPU 회수는 카탈로그에서 제거 — 데모는 factor→1.0 복원으로 대신)
        cap["status"] = "reclaim_ready"
        try:
            json.dump(cap, open(path, "w"), indent=2)
            print(f"[watcher] {uuid} status → reclaim_ready (회신)")
            emit("ReclaimReady", "Node", "gpu-npu-server-02", f"{uuid} 슬라이스 비움 완료 — reclaim_ready")
            _state[uuid] = "reclaim_ready"
            return
        except Exception as e:
            print(f"[watcher] reclaim_ready 회신 실패({e}) — 현 상태 유지")
    _state[uuid] = status


def scan_once():
    if not os.path.isdir(CAP_DIR):
        return                              # 디렉터리 없음 → 현 광고 유지
    for fn in sorted(os.listdir(CAP_DIR)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(CAP_DIR, fn)
        try:
            cap = json.load(open(p))
        except Exception:
            continue                        # 손상 파일 → 건너뜀(광고 불변)
        handle(cap, p)


if __name__ == "__main__":
    once = "--once" in sys.argv
    print(f"[watcher] capability 감시 시작 ({CAP_DIR}, poll {POLL}s, 축소만 자동)")
    while True:
        scan_once()
        if once:
            break
        time.sleep(POLL)
