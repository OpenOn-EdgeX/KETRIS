#!/usr/bin/env python3
"""[Exp_91] 233 원격 GPU 실행 서버 — 요청 단위 실행(1GbE: 프롬프트 in / 결과 out, 페이로드 작음).
   기준 워크로드 = resnet50 inference fp16 (catalog reference_workload, weights=None=다운로드 불요).
   232의 게이트(gate_core)가 이 서버로 요청을 포워딩한다. 이 서버는 실행만(제어는 게이트).
   실행: ctr -n k8s.io run --rm --gpus 0 --mount .../remote_serve.py ... kraken-infer:opt \
           python3 /work/remote_serve.py
   env: PORT(기본 8095), BATCH(기본 8), MODEL(resnet50).
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import torch, torchvision

PORT = int(os.environ.get("PORT", "8095"))
DEV = "cuda:0"
_model = None
_lock = __import__("threading").Lock()


def load():
    global _model
    m = torchvision.models.resnet50(weights=None).to(DEV, torch.float16).eval()
    for _ in range(3):  # 워밍업
        with torch.no_grad():
            m(torch.randn(1, 3, 224, 224, device=DEV, dtype=torch.float16))
    torch.cuda.synchronize()
    _model = m
    print(f"[remote_serve] resnet50 loaded on {torch.cuda.get_device_name(0)}", flush=True)


def infer(batch):
    x = torch.randn(batch, 3, 224, 224, device=DEV, dtype=torch.float16)
    with _lock, torch.no_grad():   # 단일 GPU 직렬화(게이트가 시분할 제어)
        t0 = time.monotonic()
        _model(x)
        torch.cuda.synchronize()
        dt = time.monotonic() - t0
    return dt


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        batch = int(body.get("batch", os.environ.get("BATCH", "8")))
        t0 = time.monotonic()
        dt = infer(batch)
        out = {"batch": batch, "exec_ms": round(dt * 1000, 3),
               "srv_ms": round((time.monotonic() - t0) * 1000, 3),
               "imgs_per_s": round(batch / dt, 2), "dev": torch.cuda.get_device_name(0)}
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):   # /health
        b = json.dumps({"ok": _model is not None, "dev": torch.cuda.get_device_name(0)}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    load()
    print(f"[remote_serve] listening :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
