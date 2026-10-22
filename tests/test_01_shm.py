#!/usr/bin/env python3
"""Test 01: SHM 레이아웃 및 create/open 검증"""
import sys, os, ctypes
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))

from kraken_shm import (
    KrakenSharedState, TenantState, TenantAlloc, SchedulerPolicy,
    create_shm, open_shm, get_gpu_info, SHM_MAGIC, MAX_TENANTS,
    MODE_FREE, MODE_OVERCOMMIT,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    return cond

all_ok = True

print("=== Test 01: SHM 레이아웃 ===")

# 구조체 크기 검증
all_ok &= check("TenantState size == 56",  ctypes.sizeof(TenantState) == 56,
                f"actual={ctypes.sizeof(TenantState)}")
all_ok &= check("TenantAlloc size == 64 (v2, Exp_41)",  ctypes.sizeof(TenantAlloc) == 64,
                f"actual={ctypes.sizeof(TenantAlloc)}")
all_ok &= check("SchedulerPolicy size == 80", ctypes.sizeof(SchedulerPolicy) == 80,
                f"actual={ctypes.sizeof(SchedulerPolicy)}")
all_ok &= check("KrakenSharedState size == 1144 (v2, Exp_41)", ctypes.sizeof(KrakenSharedState) == 1144,
                f"actual={ctypes.sizeof(KrakenSharedState)}")

print("\n=== Test 01: GPU 정보 감지 ===")
sm, mem = get_gpu_info()
all_ok &= check("SM count > 0",  sm > 0,  f"sm={sm}")
all_ok &= check("mem_mb > 0",    mem > 0, f"mem={mem}MB")
print(f"  GPU: {sm} SMs, {mem} MB")

print("\n=== Test 01: SHM create/open ===")
SHM_NAME = "test01"
SHM_PATH = f"/dev/shm/kraken_{SHM_NAME}"

# 잔여 파일 정리
if os.path.exists(SHM_PATH):
    os.unlink(SHM_PATH)

shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
all_ok &= check("magic correct", shm.magic == SHM_MAGIC,
                f"0x{shm.magic:08X}")
all_ok &= check("physical_sm set", shm.physical_sm_total == 84)
all_ok &= check("mode == FREE",   shm.mode == MODE_FREE)
all_ok &= check("round_trigger_lock == 0", shm.round_trigger_lock == 0)
all_ok &= check("profiling_tenant_idx == -1", shm.profiling_tenant_idx == -1)
all_ok &= check("policy.round_duration_us == 100000",
                shm.policy.round_duration_us == 100_000)

# open 테스트 (같은 파일에 재연결)
shm2, mm2 = open_shm(SHM_NAME)
all_ok &= check("re-open magic", shm2.magic == SHM_MAGIC)
all_ok &= check("re-open physical_sm", shm2.physical_sm_total == 84)

os.unlink(SHM_PATH)
print(f"\n{'전체 통과' if all_ok else '실패 있음'}")
sys.exit(0 if all_ok else 1)
