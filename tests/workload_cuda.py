#!/usr/bin/env python3
"""
custom CUDA 커널 기반 워크로드.
cuLaunchKernel을 PLT(ELF 링크)를 통해 직접 호출 → gating_lib.so hook 검증용.
cuBLAS를 사용하지 않으므로 LD_PRELOAD hook이 정상 동작함.

환경변수:
  KRAKEN_TENANT    : 테넌트 ID
  KRAKEN_GROUP     : 그룹 ID
  WORKLOAD_ITERS  : 반복 횟수 (기본 200)
  WORKLOAD_SIZE   : 행렬 크기 (기본 512, cuBLAS 없으므로 크기 제한)
  WORKLOAD_REPORT : 통계 출력 (1이면 활성)
"""
import os, sys, ctypes, time
from pathlib import Path

# libcudart를 RTLD_GLOBAL로 먼저 로드.
# gating_lib.so의 LAZY_RESOLVE(cudaMalloc)가 dlsym(RTLD_NEXT, ...)로
# libcudart의 실제 cudaMalloc을 찾으려면 RTLD_GLOBAL이 필요.
try:
    ctypes.CDLL("libcudart.so", mode=ctypes.RTLD_GLOBAL)
except OSError:
    try:
        ctypes.CDLL("libcudart.so.12", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass

ITERS  = int(os.environ.get("WORKLOAD_ITERS", "200"))
SIZE   = int(os.environ.get("WORKLOAD_SIZE",  "512"))
REPORT = os.environ.get("WORKLOAD_REPORT", "0") == "1"
TENANT = os.environ.get("KRAKEN_TENANT", "unknown")

# custom CUDA 커널 .so 로드
_so_path = Path(__file__).parent / "workload_cuda_kernel.so"
if not _so_path.exists():
    print(f"[workload_cuda] {_so_path} 없음. 먼저 빌드하세요.", file=sys.stderr)
    sys.exit(1)

_lib = ctypes.CDLL(str(_so_path))
_lib.workload_init.restype  = ctypes.c_int
_lib.workload_init.argtypes = [ctypes.c_int,
                                ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_void_p)]
_lib.workload_run.restype  = None
_lib.workload_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_int]
_lib.workload_free.restype  = None
_lib.workload_free.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

# GPU 메모리 초기화
pA = ctypes.c_void_p(); pB = ctypes.c_void_p(); pC = ctypes.c_void_p()
rc = _lib.workload_init(SIZE,
                        ctypes.byref(pA),
                        ctypes.byref(pB),
                        ctypes.byref(pC))
if rc != 0:
    print(f"[workload_cuda] GPU 초기화 실패: rc={rc}", file=sys.stderr)
    sys.exit(1)

# 실행
t0 = time.monotonic()
for _ in range(ITERS):
    _lib.workload_run(pA, pB, pC, SIZE)

# cudaDeviceSynchronize 없이 동기화: libcudart 직접 호출
_cudart = ctypes.CDLL("libcudart.so", use_errno=True)
_cudart.cudaDeviceSynchronize()

elapsed = time.monotonic() - t0

_lib.workload_free(pA, pB, pC)

if REPORT:
    print(f"[{TENANT}] {ITERS} iters, {elapsed:.2f}s, "
          f"{ITERS/elapsed:.1f} iter/s, size={SIZE}x{SIZE}")
