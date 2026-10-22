#!/usr/bin/env python3
"""
실제 GPT-2 추론 워크로드 (gating_lib.so 호환)
모델: GPT-2 small (117M params, n_layer=12, n_embd=768)
환경변수:
  KRAKEN_TENANT    : 테넌트 ID
  KRAKEN_GROUP     : 그룹 ID
  WORKLOAD_ITERS  : forward pass 횟수 (기본 1000)
  WORKLOAD_SEQ    : 입력 시퀀스 길이 (기본 128)
  WORKLOAD_REPORT : 1이면 완료 후 통계 출력
"""
import os, sys, time
import torch

ITERS  = int(os.environ.get("WORKLOAD_ITERS", "1000"))
SEQ    = int(os.environ.get("WORKLOAD_SEQ",   "128"))
REPORT = os.environ.get("WORKLOAD_REPORT", "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT", "GPT2")

if not torch.cuda.is_available():
    print("[workload_gpt2] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

# ── 모델 로드 (LD_PRELOAD 전에 gating 안 걸리는 초기화 단계) ────────────────
import warnings
warnings.filterwarnings("ignore")
from transformers import GPT2Model, GPT2Config

cfg = GPT2Config(n_layer=12, n_head=12, n_embd=768)
model = GPT2Model(cfg).to(device).eval()

# 더미 입력 사전 생성 (Python 오버헤드 최소화)
inputs = [torch.randint(0, 50257, (1, SEQ), device=device) for _ in range(10)]
# 워밍업
with torch.no_grad():
    for x in inputs[:3]:
        model(x)
torch.cuda.synchronize()

# ── 배리어: 모든 워크로드 로드 완료 후 동시 시작 ─────────────────────────────
BARRIER = os.environ.get("WORKLOAD_BARRIER", "")
READY   = os.environ.get("WORKLOAD_READY",   "")
if READY:
    open(READY, "w").close()       # "나 준비됐어" 알림
if BARRIER:
    while not os.path.exists(BARRIER):
        time.sleep(0.05)           # 시작 신호 대기

t_start = time.monotonic()
for i in range(ITERS):
    x = inputs[i % len(inputs)]
    with torch.no_grad():
        model(x)
    if i % 100 == 99:
        torch.cuda.synchronize()

torch.cuda.synchronize()
elapsed = time.monotonic() - t_start

if REPORT:
    print(f"[{TENANT}] GPT-2  {ITERS} iters  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} iter/s  {elapsed/ITERS*1000:.1f}ms/iter)")
