#!/usr/bin/env python3
"""
실제 BERT-base 추론 워크로드 (gating_lib.so 호환)
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
TENANT = os.environ.get("KRAKEN_TENANT", "BERT")

if not torch.cuda.is_available():
    print("[workload_bert] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

import warnings
warnings.filterwarnings("ignore")
from transformers import BertModel, BertConfig

cfg   = BertConfig(num_hidden_layers=12, hidden_size=768, num_attention_heads=12)
model = BertModel(cfg).to(device).eval()

inputs = [torch.randint(0, 30522, (1, SEQ), device=device) for _ in range(10)]
with torch.no_grad():
    for x in inputs[:3]:
        model(x)
torch.cuda.synchronize()

BARRIER = os.environ.get("WORKLOAD_BARRIER", "")
READY   = os.environ.get("WORKLOAD_READY",   "")
if READY:
    open(READY, "w").close()
if BARRIER:
    while not os.path.exists(BARRIER):
        time.sleep(0.05)

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
    print(f"[{TENANT}] BERT-base  {ITERS} iters  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} iter/s  {elapsed/ITERS*1000:.1f}ms/iter)")
