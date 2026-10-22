#!/usr/bin/env python3
"""
Test 03: gating_lib.so LD_PRELOAD 초기화 테스트
- 라이브러리가 정상 로드되는지
- 환경변수 없이 실행 시 graceful fallback
- KRAKEN_TENANT 설정 시 SHM 연결 시도
"""
import sys, os, subprocess, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
ROOT         = os.path.join(os.path.dirname(__file__), '..')
LIB          = os.path.join(ROOT, 'runtime', 'gating_lib.so')
WORKLOAD     = os.path.join(os.path.dirname(__file__), 'workload_gemm.py')
WORKLOAD_CUDA = os.path.join(os.path.dirname(__file__), 'workload_cuda.py')

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    return cond

all_ok = True

print("=== Test 03: gating_lib.so 존재 확인 ===")
all_ok &= check("gating_lib.so exists", os.path.exists(LIB), LIB)

print("\n=== Test 03: 환경변수 없이 로드 (graceful fallback) ===")
env = os.environ.copy()
env.pop("KRAKEN_TENANT", None)
env.pop("KRAKEN_GROUP",  None)
env["LD_PRELOAD"] = LIB
env["WORKLOAD_ITERS"] = "5"
env["WORKLOAD_SIZE"]  = "512"

r = subprocess.run(
    [sys.executable, WORKLOAD],
    env=env, capture_output=True, text=True, timeout=30
)
stderr = r.stderr
all_ok &= check("KRAKEN_TENANT 없음 경고 출력",
                "KRAKEN_TENANT" in stderr or "없음" in stderr,
                repr(stderr[:120]))
all_ok &= check("프로세스 정상 종료 (rc=0)", r.returncode == 0,
                f"rc={r.returncode}\nstderr={stderr[:200]}")

print("\n=== Test 03: SHM 없이 로드 (shm 연결 실패 fallback) ===")
# SHM이 없는 상태에서 KRAKEN_TENANT 설정
env2 = os.environ.copy()
env2["LD_PRELOAD"]   = LIB
env2["KRAKEN_TENANT"] = "test_nogroup"
env2["KRAKEN_GROUP"]  = "nonexistent_group_xyz"
env2["WORKLOAD_ITERS"] = "3"
env2["WORKLOAD_SIZE"]  = "256"

r2 = subprocess.run(
    [sys.executable, WORKLOAD],
    env=env2, capture_output=True, text=True, timeout=30
)
stderr2 = r2.stderr
all_ok &= check("SHM 연결 실패 메시지 출력",
                "shm" in stderr2.lower() or "연결" in stderr2 or "실패" in stderr2,
                repr(stderr2[:120]))
all_ok &= check("SHM 실패 시도 정상 종료 (rc=0)", r2.returncode == 0,
                f"rc={r2.returncode}")

print("\n=== Test 03: SHM 있고 KRAKEN_TENANT 설정 시 정상 초기화 ===")
# Controller 없이 직접 SHM 생성 후 테스트
from kraken_shm import create_shm
from registry import Registry

shm_name = "test03"
shm_path = f"/dev/shm/kraken_{shm_name}"
if os.path.exists(shm_path):
    os.unlink(shm_path)

shm, mm = create_shm(shm_name, physical_sm=84, physical_mem_mb=49140)
reg = Registry(shm)
reg.register("T3", virtual_sm=40, virtual_mem_mb=4096, weight=1.0)

env3 = os.environ.copy()
env3["LD_PRELOAD"]   = LIB
env3["KRAKEN_TENANT"] = "T3"
env3["KRAKEN_GROUP"]  = shm_name
env3["WORKLOAD_ITERS"]  = "10"
env3["WORKLOAD_SIZE"]   = "256"
env3["WORKLOAD_REPORT"] = "1"

# workload_cuda.py: custom CUDA 커널 사용 → cudaLaunchKernel(PLT) 경로
# workload_gemm.py: torch.mm → cuBLAS → dlopen+dlsym → LD_PRELOAD 우회로 killer 캡처 불가
# matmul_kernel 이름이 기본 패턴 "matmul"에 매칭되어 killer로 판단됨
workload_target = WORKLOAD_CUDA if os.path.exists(WORKLOAD_CUDA) else WORKLOAD
r3 = subprocess.run(
    [sys.executable, workload_target],
    env=env3, capture_output=True, text=True, timeout=30
)
stderr3 = r3.stderr
stdout3 = r3.stdout

all_ok &= check("초기화 완료 메시지 출력",
                "초기화 완료" in stderr3 or "kraken" in stderr3.lower(),
                repr(stderr3[:200]))
all_ok &= check("워크로드 정상 완료 (rc=0)", r3.returncode == 0,
                f"rc={r3.returncode}\nstderr={stderr3[:200]}")

# SHM에서 killer_count 확인
all_ok &= check("killer_count > 0 (gemm 후킹 확인)",
                shm.tenants[0].killer_count > 0,
                f"killer_count={shm.tenants[0].killer_count}")
print(f"  killer_count={shm.tenants[0].killer_count}, "
      f"exec_us={shm.tenants[0].total_exec_us}")

os.unlink(shm_path)

print(f"\n{'전체 통과' if all_ok else '실패 있음'}")
sys.exit(0 if all_ok else 1)
