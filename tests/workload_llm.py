#!/usr/bin/env python3
"""
LLM-style 워크로드 (gating_lib.so 호환 — GPU 집중형)
Prefill 단계(대형 matmul) + Decode 단계(소형 반복 matmul)를
Python 오버헤드 없이 GPU 커널을 연속으로 실행함.

환경변수:
  KRAKEN_TENANT    : 테넌트 ID (필수)
  KRAKEN_GROUP     : 그룹 ID (기본 default)
  WORKLOAD_ITERS  : 요청 수 (기본 300)
  WORKLOAD_REPORT : 1이면 완료 후 통계 출력
  CUDA_MPS_ACTIVE_THREAD_PERCENTAGE : MPS 스레드 제한
"""
import os, sys, time
import torch

ITERS   = int(os.environ.get("WORKLOAD_ITERS",  "300"))
REPORT  = os.environ.get("WORKLOAD_REPORT", "0") == "1"
TENANT  = os.environ.get("KRAKEN_TENANT", "LLM")
TRACE   = os.environ.get("WORKLOAD_TRACE_FILE", "")

_tf = None

def _log(phase):
    if _tf:
        _tf.write(f"{time.time():.4f},{phase}\n")
        _tf.flush()

if not torch.cuda.is_available():
    print("[workload_llm] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

# ── GPU 텐서 사전 할당 (Python 오버헤드 최소화) ──────────────────────────────
# Prefill 단계: 대형 행렬 (LLM FFN / Attention 시뮬레이션)
PREFILL_DIM = 4096
# Decode  단계: 소형 행렬 (token-by-token generation)
DECODE_DIM  = 1024
DECODE_STEPS = 16   # 한 요청당 decode 토큰 수

A_prefill = torch.randn(PREFILL_DIM, PREFILL_DIM, device=device)
B_prefill = torch.randn(PREFILL_DIM, PREFILL_DIM, device=device)
A_decode  = torch.randn(DECODE_DIM,  DECODE_DIM,  device=device)
B_decode  = torch.randn(DECODE_DIM,  DECODE_DIM,  device=device)
torch.cuda.synchronize()

if TRACE:
    _tf = open(TRACE, "w")

t_start = time.monotonic()

for req in range(ITERS):
    # ── Prefill: compute-intensive large matmul (→ 높은 SM 점유, 긴 커널) ──
    _log("Prefill.QKV")
    c = torch.mm(A_prefill, B_prefill)
    _log("Prefill.Attn")
    c = torch.relu(c)
    _log("Prefill.FFN")
    c = torch.mm(c, B_prefill.T)   # 두 번째 matmul (FFN 구조)

    # ── Decode: 소형 matmul 반복 (→ 많은 짧은 커널) ───────────────────────
    for _ in range(DECODE_STEPS):
        _log("Decode.QKV")
        d = torch.mm(A_decode, B_decode)
        _log("Decode.Proj")
        d = torch.relu(d)

    # 매 50 요청마다 동기화 (너무 긴 큐 방지)
    if req % 50 == 49:
        torch.cuda.synchronize()

torch.cuda.synchronize()
elapsed = time.monotonic() - t_start

if _tf:
    _tf.close()

if REPORT:
    print(f"[{TENANT}] {ITERS} reqs  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} req/s)")
