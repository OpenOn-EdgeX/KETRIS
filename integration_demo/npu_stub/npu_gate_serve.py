#!/usr/bin/env python3
# NPU 요청 게이트 serve (Exp_84). 설계서 docs/design/npu_gate_v2.md 계층② 구현.
#   공유 furiosa-llm(1 serve, PE 블록) + 앞단 HTTP 게이트(gate_core 재사용, run_fn=generate).
#   테넌트별 큐 → deficit 스케줄링 → serve 방출. 비율=분수(절대 duty) 계약(GPU와 동일).
# gate_core.py(재사용, 거의 무수정) + 크레딧 피더(로컬, feeder 계약 의미론).
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/gate")   # runtime/npu_proxy/gate_core.py hostPath 마운트
from gate_core import GateCore  # noqa: E402

MODEL = os.environ.get("NPU_MODEL", "furiosa-ai/Qwen2.5-0.5B-Instruct")
DEVICES = os.environ.get("KETI_NPU_DEVICES", "")
PORT = int(os.environ.get("GATE_PORT", "8000"))
TICK_S = float(os.environ.get("GATE_TICK_S", "0.1"))
BUDGET_US = float(os.environ.get("GATE_BUDGET_US", "300000"))  # 틱당 분배 예산


def log(ev, **kw):
    print(json.dumps({"ts": round(time.time(), 3), "event": ev, **kw}), flush=True)


log("start", model=MODEL, devices=DEVICES)
from furiosa_llm import LLM, SamplingParams  # noqa: E402
_kw = {"devices": DEVICES} if DEVICES else {}
t0 = time.time()
llm = LLM(MODEL, **_kw)
# [Exp_99] max_tokens 를 env 로 뺀다 — 요청 1건의 길이가 곧 **시간 축의 제어 양자**다.
#   Exp_84 가 비율 미달의 원인으로 "제어 양자가 요청 1건(~550ms)이라 거칠다"를 지목했으나
#   검증하지 않았다. 토큰 수를 바꿔 양자를 줄이면 비율 정확도가 오르는지 본다.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "64"))
SP = SamplingParams(max_tokens=MAX_TOKENS, temperature=0.0)
# [Exp_102] 요청별 길이 — 실제 LLM 서빙은 요청 길이가 들쭉날쭉하다. 요청 body 의
#   max_tokens 로 길이를 섞을 수 있게 한다. 미지정이면 종전대로 env 기본값을 쓴다.
_SP_CACHE = {MAX_TOKENS: SP}
def _sp(n):
    if n not in _SP_CACHE:
        _SP_CACHE[n] = SamplingParams(max_tokens=n, temperature=0.0)
    return _SP_CACHE[n]
log("loaded", elapsed=round(time.time() - t0, 3), devices=DEVICES or "auto", max_tokens=MAX_TOKENS)


class Req:
    __slots__ = ("prompt", "ev", "result", "t_submit", "t_first", "max_tokens")

    def __init__(self, prompt, max_tokens=None):
        self.prompt = prompt
        self.max_tokens = max_tokens or MAX_TOKENS
        self.ev = threading.Event()
        self.result = None
        self.t_submit = time.monotonic()
        self.t_first = None


def run_fn(req):
    # 실제 serve 방출 = furiosa-llm generate. 벽시계(us)가 gate_core 회계 단위(요청 점유시간).
    out = llm.generate(req.prompt, _sp(req.max_tokens))
    req.t_first = time.monotonic()
    req.result = out.outputs[0].text
    req.ev.set()


WC = os.environ.get("WORK_CONSERVING", "1") != "0"   # [Exp_85] 기본 work-conserving

# [Exp_102] 달성 가능성 고지 + WC 자동 전환.
#   Exp_101 Part B/D 실측: work-conserving 은 후순위 테넌트를 완전히 굶길 수 없어
#   최대/최소 비가 커지면 목표를 달성하지 못한다(9:1 요구 → 실측 3.21, 76:24 천장).
#   교차점은 비율 ≈4.0(8:2). 그 아래에서는 WC 가 처리량과 정확도를 동시에 이기고,
#   그 위에서만 non-WC 가 몫을 만든다(9:1: 36% → 70%, 대가 처리량 -39%).
#   기본은 고지만 한다(WC_AUTO=0) — 처리량 30~40% 를 말없이 내주지 않는다.
WC_THRESHOLD = float(os.environ.get("WC_THRESHOLD", "4.0"))
WC_AUTO = os.environ.get("WC_AUTO", "0") != "0"
gate = GateCore(run_fn, work_conserving=WC)


def scheduler_loop():
    while True:
        if gate.step() is None:
            time.sleep(0.001)


# 비율(분수) 피더: RATIOS env "A:0.7,B:0.3" → 틱마다 granted 를 비율대로 time_add
RATIOS = {}
for part in os.environ.get("RATIOS", "").split(","):
    if ":" in part:
        k, v = part.split(":"); RATIOS[k.strip()] = float(v)


def assess_ratios():
    """목표 비율이 WC 로 달성 가능한 범위인지 판정하고 알린다(Exp_101 Part D)."""
    if not RATIOS or len(RATIOS) < 2:
        return
    mx, mn = max(RATIOS.values()), min(RATIOS.values())
    spread = mx / mn if mn > 0 else float("inf")
    ok = spread < WC_THRESHOLD
    global WC
    if not ok and WC and WC_AUTO:
        WC = False
        gate.work_conserving = False
        log("wc_switched", spread=round(spread, 2), threshold=WC_THRESHOLD,
            reason="목표 비율이 work-conserving 달성 범위를 넘음",
            cost="총처리량 30~40% 감소 예상(Exp_101 Part D)")
    elif not ok:
        log("ratio_unachievable", spread=round(spread, 2), threshold=WC_THRESHOLD,
            note="work-conserving 에서는 목표에 미달한다(후순위를 굶길 수 없음). "
                 "WC_AUTO=1 로 전환 가능하나 총처리량 30~40% 를 잃는다.")
    else:
        log("ratio_achievable", spread=round(spread, 2), threshold=WC_THRESHOLD)


def feeder_loop():
    for name in RATIOS:
        gate.register(name)
        gate.cmd(name, "time_mode", "1")   # 게이트 무장
    while True:
        for name, frac in RATIOS.items():
            gate.cmd(name, "time_add", str(BUDGET_US * frac))  # 분수 계약: 예산×비율
        time.sleep(TICK_S)


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        tenant = self.headers.get("X-Tenant", "default")
        gate.register(tenant)   # 미선언 테넌트도 등록(unlimited=게이트 없음이 기본)
        req = Req(body.get("prompt", "Hello"), body.get("max_tokens"))
        gate.submit(tenant, req)
        req.ev.wait(timeout=120)
        ttft_ms = round((req.t_first - req.t_submit) * 1000, 1) if req.t_first else None
        out = json.dumps({"text": req.result, "ttft_ms": ttft_ms, "tenant": tenant}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_GET(self):
        if self.path.rstrip("/") == "/stats":
            out = json.dumps(gate.stats()).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


# [Exp_100] 스케줄러 루프 수 — 게이트의 **직렬성**이 자원을 놀리는지 판정하기 위해 env 로 뺀다.
#   step() 은 락 안에서 선택하고 run_fn 은 락 밖에서 실행하므로 다중 스레드가 안전하다.
#   기본 1 = 종전 동작(직렬). 8PE 는 dp=2 로 두 그룹이 뜨는데(로그 tp=4,pp=1,dp=2)
#   요청을 하나씩만 넣으면 한 그룹이 논다는 가설을 이걸로 검증한다.
GATE_WORKERS = int(os.environ.get("GATE_WORKERS", "1"))
for _ in range(GATE_WORKERS):
    threading.Thread(target=scheduler_loop, daemon=True).start()
if RATIOS:
    assess_ratios()
    threading.Thread(target=feeder_loop, daemon=True).start()
    log("gated", ratios=RATIOS, work_conserving=WC)
else:
    log("ungated")
log("serving", port=PORT, workers=GATE_WORKERS)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
