#!/usr/bin/env python3
"""
실제 ResNet-50 추론 워크로드 (gating_lib.so 호환)
환경변수:
  KRAKEN_TENANT    : 테넌트 ID
  KRAKEN_GROUP     : 그룹 ID
  WORKLOAD_ITERS  : forward pass 횟수 (기본 300)
  WORKLOAD_BATCH  : 배치 크기 (기본 32)
  WORKLOAD_REPORT : 1이면 완료 후 통계 출력
"""
import os, sys, time
import torch

ITERS  = int(os.environ.get("WORKLOAD_ITERS", "300"))
BATCH  = int(os.environ.get("WORKLOAD_BATCH", "32"))
REPORT = os.environ.get("WORKLOAD_REPORT", "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT", "ResNet")
TRACE  = os.environ.get("WORKLOAD_TRACE_FILE", "")

_tf = None

def _log(phase):
    if _tf:
        _tf.write(f"{time.time():.4f},{phase}\n")
        _tf.flush()

if not torch.cuda.is_available():
    print("[workload_resnet] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

import warnings
warnings.filterwarnings("ignore")
from torchvision.models import resnet50

model = resnet50(weights=None).to(device).eval()

# 더미 이미지 배치 사전 생성
imgs = [torch.randn(BATCH, 3, 224, 224, device=device) for _ in range(5)]
# 워밍업
with torch.no_grad():
    for x in imgs[:2]:
        model(x)
torch.cuda.synchronize()

# ── 오퍼레이터 추적 ───────────────────────────────────────────────────────────
if TRACE:
    _tf = open(TRACE, "w")
    model.conv1.register_forward_pre_hook(lambda m, i: _log("Conv1_BN"))
    model.layer1.register_forward_pre_hook(lambda m, i: _log("Layer1_Res"))
    model.layer2.register_forward_pre_hook(lambda m, i: _log("Layer2_Res"))
    model.layer3.register_forward_pre_hook(lambda m, i: _log("Layer3_Res"))
    model.layer4.register_forward_pre_hook(lambda m, i: _log("Layer4_Res"))
    model.avgpool.register_forward_pre_hook(lambda m, i: _log("AvgPool"))
    model.fc.register_forward_pre_hook(lambda m, i: _log("FC"))

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
    print(f"[{TENANT}] ResNet-50 batch={BATCH}  {ITERS} iters  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} iter/s  {elapsed/ITERS*1000:.1f}ms/iter)")
