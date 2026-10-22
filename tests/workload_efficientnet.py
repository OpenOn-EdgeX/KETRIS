#!/usr/bin/env python3
"""
EfficientNet-B0 추론 워크로드 (gating_lib.so 호환)
환경변수:
  KRAKEN_TENANT       : 테넌트 ID
  KRAKEN_GROUP        : 그룹 ID
  WORKLOAD_ITERS     : forward pass 횟수 (기본 300)
  WORKLOAD_BATCH     : 배치 크기 (기본 16)
  WORKLOAD_REPORT    : 1이면 완료 후 통계 출력
  WORKLOAD_TRACE_FILE: 오퍼레이터 로그 파일 경로 (선택)
  WORKLOAD_BARRIER   : 시작 배리어 파일 경로
  WORKLOAD_READY     : 준비 완료 알림 파일 경로
"""
import os, sys, time
import torch

ITERS  = int(os.environ.get("WORKLOAD_ITERS",  "300"))
BATCH  = int(os.environ.get("WORKLOAD_BATCH",  "16"))
REPORT = os.environ.get("WORKLOAD_REPORT",  "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT",    "EfficientNet")
TRACE  = os.environ.get("WORKLOAD_TRACE_FILE", "")

if not torch.cuda.is_available():
    print("[workload_efficientnet] CUDA 없음", file=sys.stderr)
    sys.exit(1)

device = torch.device("cuda")

import warnings
warnings.filterwarnings("ignore")
from torchvision.models import efficientnet_b0

model = efficientnet_b0(weights=None).to(device).eval()

imgs = [torch.randn(BATCH, 3, 224, 224, device=device) for _ in range(5)]
with torch.no_grad():
    for x in imgs[:2]:
        model(x)
torch.cuda.synchronize()

# ── 오퍼레이터 추적 ───────────────────────────────────────────────────────────
_tf = None

def _log(phase):
    if _tf:
        _tf.write(f"{time.time():.4f},{phase}\n")
        _tf.flush()

# EfficientNet-B0 features[0]: stem Conv, features[1-7]: MBConv blocks, features[8]: head conv
_BLOCK_LABELS = [
    "StemConv",       # features[0]
    "MBConv1",        # features[1]
    "MBConv6_2",      # features[2]
    "MBConv6_3",      # features[3]
    "MBConv6_4",      # features[4]
    "MBConv6_5",      # features[5]
    "MBConv6_6",      # features[6]
    "MBConv6_7",      # features[7]
    "HeadConv",       # features[8]
]

if TRACE:
    _tf = open(TRACE, "w")
    for idx, feat in enumerate(model.features):
        label = _BLOCK_LABELS[idx] if idx < len(_BLOCK_LABELS) else f"Block{idx}"
        feat.register_forward_pre_hook(
            lambda m, i, lbl=label: _log(lbl))
    model.classifier.register_forward_pre_hook(lambda m, i: _log("Classifier"))

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
    print(f"[{TENANT}] EfficientNet-B0  batch={BATCH}  {ITERS} iters  {elapsed:.2f}s  "
          f"({ITERS/elapsed:.1f} iter/s  {elapsed/ITERS*1000:.1f}ms/iter)")
