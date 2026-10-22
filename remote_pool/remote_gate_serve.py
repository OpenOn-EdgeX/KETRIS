#!/usr/bin/env python3
"""[Exp_91] 232 원격 게이트 — 232 파드/클라이언트 → (이 게이트, gate_core WC 시간제어) → 233 실행서버.
   ★gate_core 재사용(Exp_85 WC). run_fn = 233 으로 HTTP 포워딩(요청 단위). npu_gate_serve 미러.
   제어점은 232(요청 유입), 실행은 233. 두 테넌트 비율 분할 + 네트워크 왕복이 제어에 얹힘.
   env: PORT(8096), REMOTE_URL(http://10.0.4.233:8095/infer), RATIOS("A:0.7,B:0.3"),
        BUDGET_US(100000), TICK_S(0.05), WORK_CONSERVING(1).
"""
import json, os, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# gate_core 는 pooling-kernel-scheduling(runtime/npu_proxy)에 있다. 배포 형태마다
# 위치가 다르므로 env 로 지정하고, 없으면 관례적 위치를 순서대로 탐색한다.
#   ① KRAKEN_GATE_CORE_DIR  ② 컨테이너 이미지 레이아웃  ③ 모노레포 상대 경로
_GC_CANDS = [
    os.environ.get("KRAKEN_GATE_CORE_DIR"),
    "/opt/kraken/npu_proxy",
    "/gate",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "npu_proxy"),
]
for _c in _GC_CANDS:
    if _c and os.path.isdir(_c):
        sys.path.insert(0, _c)
        break
from gate_core import GateCore

REMOTE = os.environ.get("REMOTE_URL", "http://10.0.4.233:8095/infer")
PORT = int(os.environ.get("PORT", "8096"))
BUDGET_US = int(os.environ.get("BUDGET_US", "100000"))
TICK_S = float(os.environ.get("TICK_S", "0.05"))
WC = os.environ.get("WORK_CONSERVING", "1") != "0"


class Req:
    def __init__(self, batch):
        self.batch = batch; self.ev = threading.Event(); self.out = None


def run_fn(req):
    """★원격 실행: 233 서버로 포워딩(요청 단위). gate_core 가 이 호출의 wall-clock 을 청구."""
    body = json.dumps({"batch": req.batch}).encode()
    r = urllib.request.Request(REMOTE, data=body, headers={"Content-Type": "application/json"})
    req.out = json.load(urllib.request.urlopen(r, timeout=60))
    req.ev.set()


gate = GateCore(run_fn, work_conserving=WC)


def scheduler():
    while True:
        if gate.step() is None:
            time.sleep(0.001)


def feeder():
    ratios = {}
    for part in os.environ.get("RATIOS", "").split(","):
        if ":" in part:
            n, f = part.split(":"); ratios[n.strip()] = float(f)
    for n in ratios:
        gate.register(n); gate.cmd(n, "time_mode", "1")
    while True:
        for n, f in ratios.items():
            gate.cmd(n, "time_add", str(int(BUDGET_US * f)))
        time.sleep(TICK_S)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        tenant = self.headers.get("X-Tenant", "default")
        gate.register(tenant)
        req = Req(int(body.get("batch", 8)))
        t0 = time.monotonic()
        gate.submit(tenant, req)
        req.ev.wait(timeout=120)
        out = dict(req.out or {}); out["gate_ms"] = round((time.monotonic() - t0) * 1000, 3)
        out["tenant"] = tenant
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        b = json.dumps(gate.stats() if hasattr(gate, "stats") else {"ok": True}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    if os.environ.get("RATIOS"):
        threading.Thread(target=feeder, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"[remote_gate] :{PORT} → {REMOTE}  RATIOS={os.environ.get('RATIOS','(none)')} WC={WC}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
