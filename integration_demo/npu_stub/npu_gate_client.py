#!/usr/bin/env python3
# NPU 게이트 부하 클라이언트 (Exp_84). 테넌트별 요청을 게이트에 던지고 처리량/TTFT 측정.
# usage: npu_gate_client.py <url> <duration_s> <tenant:concurrency,...>
import json, os, sys, threading, time, urllib.request

URL = sys.argv[1]
DUR = float(sys.argv[2])
SPEC = sys.argv[3]   # "A:2,B:2" — 테넌트별 동시 연결 수(백프레셔 부하)
PROMPT = "What is the capital of France? Answer in one sentence."
# [Exp_102] 요청 길이 섞기 — env MIX="16,64,256" 이면 워커가 순환하며 그 길이로 보낸다.
#   랜덤이 아니라 결정적 순환이다(재현성). 미지정이면 서버 기본값(길이 고정).
MIX = [int(x) for x in os.environ.get("MIX", "").split(",") if x.strip()]

results = {}
lock = threading.Lock()


def worker(tenant):
    n, ttfts = 0, []
    body = json.dumps({"prompt": PROMPT}).encode()
    deadline = time.monotonic() + DUR
    i = 0
    while time.monotonic() < deadline:
        if MIX:
            body = json.dumps({"prompt": PROMPT, "max_tokens": MIX[i % len(MIX)]}).encode()
            i += 1
        req = urllib.request.Request(URL, data=body,
                                     headers={"Content-Type": "application/json", "X-Tenant": tenant})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            n += 1
            if d.get("ttft_ms") is not None:
                ttfts.append(d["ttft_ms"])
        except Exception:
            pass
    with lock:
        r = results.setdefault(tenant, {"done": 0, "ttfts": []})
        r["done"] += n
        r["ttfts"].extend(ttfts)


threads = []
for part in SPEC.split(","):
    tenant, conc = part.split(":")
    for _ in range(int(conc)):
        th = threading.Thread(target=worker, args=(tenant,))
        threads.append(th)
for th in threads:
    th.start()
t0 = time.monotonic()
for th in threads:
    th.join()
el = time.monotonic() - t0

out = {}
for tenant, r in results.items():
    tt = sorted(r["ttfts"])
    out[tenant] = {"done": r["done"], "rps": round(r["done"] / el, 2),
                   "ttft_p50": tt[len(tt) // 2] if tt else None,
                   "ttft_p95": tt[int(len(tt) * 0.95)] if tt else None,
                   "ttft_p99": tt[min(len(tt) - 1, int(len(tt) * 0.99))] if tt else None}
tot = sum(r["done"] for r in results.values())
print(json.dumps({"elapsed": round(el, 2), "total": tot, "tenants": out}))
