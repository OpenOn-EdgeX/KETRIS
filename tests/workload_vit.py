#!/usr/bin/env python3
"""
ViT-B/16 추론 워크로드 (gating_lib.so 호환)
환경변수:
  KRAKEN_TENANT       : 테넌트 ID
  KRAKEN_GROUP        : 그룹 ID
  WORKLOAD_ITERS     : forward pass 횟수 (기본 200)
  WORKLOAD_BATCH     : 배치 크기 (기본 8)
  WORKLOAD_REPORT    : 1이면 완료 후 통계 출력
  WORKLOAD_TRACE_FILE: 오퍼레이터 로그 파일 경로 (선택)
  WORKLOAD_BARRIER   : 시작 배리어 파일 경로
  WORKLOAD_READY     : 준비 완료 알림 파일 경로
"""
import os, sys, time
import torch

ITERS  = int(os.environ.get("WORKLOAD_ITERS",  "200"))
BATCH  = int(os.environ.get("WORKLOAD_BATCH",  "8"))
REPORT = os.environ.get("WORKLOAD_REPORT",  "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT",    "ViT")
TRACE  = os.environ.get("WORKLOAD_TRACE_FILE", "")

if not torch.cuda.is_available():
    print("[workload_vit] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

import warnings
warnings.filterwarnings("ignore")
from torchvision.models import vit_b_16

model = vit_b_16(weights=None).to(device).eval()

imgs = [torch.randn(BATCH, 3, 224, 224, device=device) for _ in range(5)]
with torch.no_grad():
    for x in imgs[:2]:
        model(x)
torch.cuda.synchronize()

# ── 오퍼레이터 추적 ───────────────────────────────────────────────────────────
_tf    = None
_t0_tr = 0.0

def _log(phase):
    if _tf:
        _tf.write(f"{time.time():.4f},{phase}\n")
        _tf.flush()

if TRACE:
    _tf = open(TRACE, "w")
    # Patch embedding
    model.conv_proj.register_forward_pre_hook(
        lambda m, i: _log("PatchEmbed"))
    # Encoder blocks (Attention / MLP per block)
    for idx, layer in enumerate(model.encoder.layers):
        layer.ln_1.register_forward_pre_hook(
            lambda m, i, n=idx: _log(f"Attn{n}"))
        layer.ln_2.register_forward_pre_hook(
            lambda m, i, n=idx: _log(f"MLP{n}"))
    # Classification head
    model.heads.register_forward_pre_hook(lambda m, i: _log("Head"))

BARRIER = os.environ.get("WORKLOAD_BARRIER", "")
READY   = os.environ.get("WORKLOAD_READY",   "")
if READY:
    open(READY, "w").close()
if BARRIER:
    while not os.path.exists(BARRIER):
        time.sleep(0.05)

t_start = time.monotonic()
for i in range(ITERS):
    x = imgs[i % len(imgs)]
    with torch.no_grad():
        model(x)
    if i % 50 == 49:
        torch.cuda.synchronize()

torch.cuda.synchronize()
elapsed = time.monotonic() - t_start

if _tf:
    _tf.close()

if REPORT:
    print(f"[{TENANT}] ViT-B/16  batch={BATCH}  {ITERS} iters  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} iter/s  {elapsed/ITERS*1000:.1f}ms/iter)")
