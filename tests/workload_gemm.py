#!/usr/bin/env python3
"""
테스트용 GEMM 워크로드.
LD_PRELOAD=gating_lib.so로 실행하면 killer 게이팅이 개입됨.

환경변수:
  KRAKEN_TENANT   : 테넌트 ID (필수)
  KRAKEN_GROUP    : 그룹 ID (기본 default)
  WORKLOAD_ITERS : 반복 횟수 (기본 200)
  WORKLOAD_SIZE  : 행렬 크기 (기본 2048)
  WORKLOAD_REPORT: 완료 후 통계 출력 (1이면 활성)
"""
import os, sys, time
import torch

ITERS = int(os.environ.get("WORKLOAD_ITERS", "200"))
SIZE  = int(os.environ.get("WORKLOAD_SIZE",  "2048"))
REPORT = os.environ.get("WORKLOAD_REPORT", "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT", "unknown")

if not torch.cuda.is_available():
    print("[workload] CUDA 없음, 종료")
    sys.exit(1)

device = torch.device("cuda")
a = torch.randn(SIZE, SIZE, device=device, dtype=torch.float32)
b = torch.randn(SIZE, SIZE, device=device, dtype=torch.float32)
torch.cuda.synchronize()

t0 = time.monotonic()
for i in range(ITERS):
    c = torch.mm(a, b)
    # 추가 연산 (conv 대용)
    c = torch.relu(c)
    c = c @ b.T
    if i % 50 == 0:
        torch.cuda.synchronize()

torch.cuda.synchronize()
elapsed = time.monotonic() - t0

if REPORT:
    throughput = ITERS / elapsed
    print(f"[{TENANT}] {ITERS} iters, {elapsed:.2f}s, "
          f"{throughput:.1f} iter/s, size={SIZE}x{SIZE}")
