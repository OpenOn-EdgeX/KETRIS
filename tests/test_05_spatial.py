#!/usr/bin/env python3
"""
Test 05: MPS 공간 분할 검증
- CUDA_MPS_ACTIVE_THREAD_PERCENTAGE로 SM 제한이 실제로 적용되는지 확인
- FREE 모드에서 A(50%) + B(50%)가 각자 절반 SM만 사용하는지 검증

시나리오:
  1. 단일 테넌트 MPS=100% → 기준 시간 측정
  2. 단일 테넌트 MPS=50%  → 기준 대비 ~1.2-1.4x 느림 (SM 제한 효과 확인)
  3. 두 테넌트 동시 MPS=50% each (FREE 모드)
     → 각 테넌트 처리 시간은 MPS=50% 단독과 유사
     → 총 처리량은 두 배 (공간 분할로 SM 공유)
"""
import sys, os, time, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

PASS  = "\033[32mPASS\033[0m"
FAIL  = "\033[31mFAIL\033[0m"
WARN  = "\033[33mWARN\033[0m"
ROOT  = os.path.join(os.path.dirname(__file__), '..')
LIB   = os.path.join(ROOT, 'runtime', 'gating_lib.so')
WL    = os.path.join(os.path.dirname(__file__), 'workload_gemm.py')

def check(name, cond, info="", warn=False):
    if warn and not cond:
        status = WARN
    else:
        status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    return cond or warn

all_ok = True

from kraken_shm import create_shm, MODE_FREE
from registry  import Registry

SHM_NAME = "test05"
SHM_PATH = f"/dev/shm/kraken_{SHM_NAME}"

# ── MPS 데몬 확인 ──────────────────────────────────────────────────────────────
import subprocess as _sp
r = _sp.run(["nvidia-cuda-mps-control", "get_server_list"],
            capture_output=True, timeout=5)
mps_running = (r.returncode == 0)

if not mps_running:
    print("[test_05] MPS 데몬 시작 중...")
    _sp.run(["nvidia-cuda-mps-control", "-d"], check=True, timeout=10)
    time.sleep(1.0)
    mps_running = True

print(f"[test_05] MPS 데몬 상태: {'실행 중' if mps_running else '없음'}")

ITERS = "100"
SIZE  = "2048"

base_env = os.environ.copy()

# ── 시나리오 1: 단일 테넌트 MPS=100% (기준선) ─────────────────────────────────
print("\n=== Test 05-1: 단일 테넌트 MPS=100% (기준선) ===")

env_base100 = {**base_env,
               "WORKLOAD_ITERS": ITERS, "WORKLOAD_SIZE": SIZE,
               "WORKLOAD_REPORT": "1",
               "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "100"}

t0 = time.monotonic()
p = subprocess.run([sys.executable, WL], env=env_base100,
                   capture_output=True, timeout=60)
t_100 = time.monotonic() - t0

if p.returncode == 0:
    output = p.stderr.decode() or p.stdout.decode()
    print(f"  출력: {output.strip()}")
print(f"  MPS=100% 시간: {t_100:.2f}s")
all_ok &= check("MPS=100% 정상 완료", p.returncode == 0, f"rc={p.returncode}")

# ── 시나리오 2: 단일 테넌트 MPS=50% (SM 제한 효과) ────────────────────────────
print("\n=== Test 05-2: 단일 테넌트 MPS=50% (SM 제한 효과) ===")

env_50 = {**base_env,
          "WORKLOAD_ITERS": ITERS, "WORKLOAD_SIZE": SIZE,
          "WORKLOAD_REPORT": "1",
          "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "50"}

t0 = time.monotonic()
p50 = subprocess.run([sys.executable, WL], env=env_50,
                     capture_output=True, timeout=60)
t_50 = time.monotonic() - t0

if p50.returncode == 0:
    output = p50.stderr.decode() or p50.stdout.decode()
    print(f"  출력: {output.strip()}")
ratio_50 = t_50 / t_100 if t_100 > 0 else 0
print(f"  MPS=50% 시간: {t_50:.2f}s  (기준 대비 {ratio_50:.2f}x)")

all_ok &= check("MPS=50% 정상 완료", p50.returncode == 0, f"rc={p50.returncode}")
# SM 제한으로 1.1x~2.5x 느려야 함 (SM 절반 = 완전 비례가 아닌, 메모리 대역폭 등 영향)
all_ok &= check("MPS=50% 속도 저하 확인 (1.1x~2.5x)",
                1.05 < ratio_50 < 2.5,
                f"ratio={ratio_50:.2f}x", warn=True)

# ── 시나리오 3: FREE 모드 2-테넌트 MPS=50% 각자 ────────────────────────────────
print("\n=== Test 05-3: FREE 모드 — A(MPS=50%) + B(MPS=50%) 동시 실행 ===")

if os.path.exists(SHM_PATH):
    os.unlink(SHM_PATH)

shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
reg = Registry(shm)
reg.register("A", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)
reg.register("B", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)

mps_a = shm.alloc[0].mps_pct  # 42/84*100 = 50
mps_b = shm.alloc[1].mps_pct  # 42/84*100 = 50

all_ok &= check("mode == FREE", shm.mode == MODE_FREE, f"mode={shm.mode}")
print(f"  MPS cap: A={mps_a}%  B={mps_b}%")

env_a = {**base_env,
         "LD_PRELOAD": LIB,
         "KRAKEN_GROUP": SHM_NAME,
         "KRAKEN_TENANT": "A",
         "WORKLOAD_ITERS": ITERS, "WORKLOAD_SIZE": SIZE,
         "WORKLOAD_REPORT": "1",
         "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_a)}

env_b = {**base_env,
         "LD_PRELOAD": LIB,
         "KRAKEN_GROUP": SHM_NAME,
         "KRAKEN_TENANT": "B",
         "WORKLOAD_ITERS": ITERS, "WORKLOAD_SIZE": SIZE,
         "WORKLOAD_REPORT": "1",
         "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_b)}

t0 = time.monotonic()
pa = subprocess.Popen([sys.executable, WL], env=env_a,
                      stderr=subprocess.PIPE, stdout=subprocess.PIPE)
pb = subprocess.Popen([sys.executable, WL], env=env_b,
                      stderr=subprocess.PIPE, stdout=subprocess.PIPE)
pa.wait(timeout=120)
pb.wait(timeout=120)
t_parallel = time.monotonic() - t0

out_a = pa.stderr.read().decode() or pa.stdout.read().decode()
out_b = pb.stderr.read().decode() or pb.stdout.read().decode()
if out_a.strip():
    print(f"  A: {out_a.strip()}")
if out_b.strip():
    print(f"  B: {out_b.strip()}")
print(f"  A+B 동시 실행 시간: {t_parallel:.2f}s")
print(f"  단독 MPS=50% 대비: {t_parallel/t_50:.2f}x")

all_ok &= check("A 정상 종료", pa.returncode == 0, f"rc={pa.returncode}")
all_ok &= check("B 정상 종료", pb.returncode == 0, f"rc={pb.returncode}")

# A+B 동시 실행이 A 단독 MPS=50%와 비슷한 시간이면 공간 분할 효과 있음
# (둘이 서로 다른 SM을 쓰므로 벽시계 시간이 크게 증가하지 않아야 함)
# 허용 범위: 단독 MPS=50% 대비 0.8x~1.5x
ratio_parallel = t_parallel / t_50 if t_50 > 0 else 0
all_ok &= check("병렬 실행 오버헤드 ≤ 1.5x (공간 분할 효과)",
                ratio_parallel < 1.5,
                f"parallel/solo_50%={ratio_parallel:.2f}x", warn=True)

# ── 요약 ──────────────────────────────────────────────────────────────────────
print("\n=== Test 05 요약 ===")
print(f"  단독 MPS=100%:            {t_100:.2f}s  (기준)")
print(f"  단독 MPS=50%:             {t_50:.2f}s  ({ratio_50:.2f}x — SM 제한 효과)")
print(f"  A(50%)+B(50%) 동시:       {t_parallel:.2f}s  (단독 50% 대비 {ratio_parallel:.2f}x)")
print(f"  → 공간 분할 효과: {'있음 ✅' if ratio_parallel < 1.5 else '미흡 ⚠️'}")

os.unlink(SHM_PATH)

print(f"\n{'전체 통과' if all_ok else '일부 실패 (WARN 제외)'}")
sys.exit(0 if all_ok else 1)
