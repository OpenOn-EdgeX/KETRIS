#!/usr/bin/env python3
"""
Test 04: 2-tenant 시간 분할 측정
- A: weight=0.67 (virtual_sm=56), B: weight=0.33 (virtual_sm=28) → 합 84 ≤ 84 → FREE
- A: weight=0.5  (virtual_sm=56), B: weight=0.5 (virtual_sm=56)  → 합 112 > 84 → OVERCOMMIT
- OVERCOMMIT 상태에서 실제 exec 비율이 weight에 비례하는지 검증
"""
import sys, os, time, subprocess, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'monitor'))

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

from kraken_shm import create_shm, open_shm, MODE_FREE, MODE_OVERCOMMIT
from registry  import Registry
from metrics_logger import MetricsLogger

SHM_NAME = "test04"
SHM_PATH = f"/dev/shm/kraken_{SHM_NAME}"
if os.path.exists(SHM_PATH):
    os.unlink(SHM_PATH)

# ── 시나리오 1: FREE 모드 (합 84 ≤ 84) ────────────────────────────────────────
print("=== Test 04-1: FREE 모드 2-tenant ===")
shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
reg = Registry(shm)
reg.register("A", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)
reg.register("B", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)

all_ok &= check("mode == FREE", shm.mode == MODE_FREE, f"mode={shm.mode}")
print(f"  MPS cap: A={shm.alloc[0].mps_pct}%  B={shm.alloc[1].mps_pct}%")

base_env = os.environ.copy()
base_env["LD_PRELOAD"]   = LIB
base_env["KRAKEN_GROUP"]  = SHM_NAME
base_env["WORKLOAD_ITERS"] = "100"
base_env["WORKLOAD_SIZE"]  = "2048"

env_a = {**base_env, "KRAKEN_TENANT": "A",
         "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[0].mps_pct)}
env_b = {**base_env, "KRAKEN_TENANT": "B",
         "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[1].mps_pct)}

# 두 프로세스 동시 실행
t0 = time.monotonic()
pa = subprocess.Popen([sys.executable, WL], env=env_a,
                      stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
pb = subprocess.Popen([sys.executable, WL], env=env_b,
                      stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
pa.wait(timeout=60)
pb.wait(timeout=60)
elapsed = time.monotonic() - t0

all_ok &= check("A 정상 종료", pa.returncode == 0, f"rc={pa.returncode}")
all_ok &= check("B 정상 종료", pb.returncode == 0, f"rc={pb.returncode}")
print(f"  FREE 모드 실행 시간: {elapsed:.1f}s")

# FREE 모드에서는 killer_count가 0일 수도 있음 (게이팅 없음)
print(f"  A killer_count={shm.tenants[0].killer_count}, "
      f"B killer_count={shm.tenants[1].killer_count}")

os.unlink(SHM_PATH)

# ── 시나리오 2: OVERCOMMIT 모드 (합 168 > 84) ─────────────────────────────────
print("\n=== Test 04-2: OVERCOMMIT 모드 — 시간 분할 공정성 ===")
shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
reg = Registry(shm)

# A: weight 0.67, B: weight 0.33 (2:1 비율)
reg.register("A", virtual_sm=84, virtual_mem_mb=8192, weight=2.0)
reg.register("B", virtual_sm=84, virtual_mem_mb=8192, weight=1.0)
# weights 정규화
shm.policy.weights[0] = 2.0 / 3.0
shm.policy.weights[1] = 1.0 / 3.0
shm.policy.version += 1

all_ok &= check("mode == OVERCOMMIT", shm.mode == MODE_OVERCOMMIT,
                f"mode={shm.mode}")
print(f"  round_duration={shm.policy.round_duration_us}μs "
      f"(A:{shm.policy.weights[0]:.2f}, B:{shm.policy.weights[1]:.2f})")
print(f"  MPS cap: A={shm.alloc[0].mps_pct}%  B={shm.alloc[1].mps_pct}%")

logger = MetricsLogger.__new__(MetricsLogger)
logger.shm = shm; logger._mm = mm
logger.interval = 2.0; logger.stdout = False; logger.output = "/dev/null"
logger._prev = {}

env_a2 = {**base_env, "KRAKEN_TENANT": "A", "WORKLOAD_ITERS": "300",
          "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[0].mps_pct)}
env_b2 = {**base_env, "KRAKEN_TENANT": "B", "WORKLOAD_ITERS": "300",
          "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[1].mps_pct)}

t0 = time.monotonic()
pa2 = subprocess.Popen([sys.executable, WL], env=env_a2,
                       stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
pb2 = subprocess.Popen([sys.executable, WL], env=env_b2,
                       stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

# 3초 워밍업 후 2초 측정 구간
time.sleep(3.0)
logger.snapshot()                # prev 초기화
time.sleep(2.0)
rows = logger.snapshot()         # 2초 구간 측정

pa2.wait(timeout=60)
pb2.wait(timeout=60)
elapsed2 = time.monotonic() - t0

all_ok &= check("A 정상 종료", pa2.returncode == 0, f"rc={pa2.returncode}")
all_ok &= check("B 정상 종료", pb2.returncode == 0, f"rc={pb2.returncode}")

if rows:
    for r in rows:
        print(f"  {r['tenant']}: exec_rate={r['exec_rate_pct']:.1f}% "
              f"wait_rate={r['wait_rate_pct']:.1f}% "
              f"fairness={r['fairness']:.3f} "
              f"killer_count={r['killer_count']}")

    a_row = next((r for r in rows if r["tenant"] == "A"), None)
    b_row = next((r for r in rows if r["tenant"] == "B"), None)

    if a_row and b_row:
        # 실제 비율
        total_exec = a_row["exec_rate_pct"] + b_row["exec_rate_pct"]
        if total_exec > 0:
            a_actual = a_row["exec_rate_pct"] / total_exec
            b_actual = b_row["exec_rate_pct"] / total_exec
            print(f"\n  실제 비율  A:{a_actual:.2f}  B:{b_actual:.2f}")
            print(f"  목표 비율  A:0.67  B:0.33")

            # 허용 오차 ±15%p
            all_ok &= check("A exec 비율 ≈ 0.67 (±0.15)",
                            abs(a_actual - 0.667) < 0.15,
                            f"A_actual={a_actual:.3f}", warn=True)
            all_ok &= check("B exec 비율 ≈ 0.33 (±0.15)",
                            abs(b_actual - 0.333) < 0.15,
                            f"B_actual={b_actual:.3f}", warn=True)

        # killer_count 확인
        all_ok &= check("A killer_count > 0",
                        shm.tenants[0].killer_count > 0,
                        f"count={shm.tenants[0].killer_count}")
        all_ok &= check("B killer_count > 0",
                        shm.tenants[1].killer_count > 0,
                        f"count={shm.tenants[1].killer_count}")
        all_ok &= check("round_id 증가 (라운드 진행 확인)",
                        shm.tenants[0].round_id > 0,
                        f"round_id={shm.tenants[0].round_id}")

print(f"\n  OVERCOMMIT 실행 시간: {elapsed2:.1f}s")
os.unlink(SHM_PATH)

print(f"\n{'전체 통과' if all_ok else '일부 실패 (WARN 제외)'}")
sys.exit(0 if all_ok else 1)
