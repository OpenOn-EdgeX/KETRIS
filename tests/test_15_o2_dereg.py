#!/usr/bin/env python3
"""Test 15: O-2 무음 절단 제거 (Exp_41) — SHM v2 레이아웃·등록/해제 결정성."""
import ctypes, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
import kraken_shm
from kraken_shm import (SHM_MAGIC, SHM_MAGIC_V1, SHM_LAYOUT_VERSION,
                       TENANT_ID_LEN, KrakenSharedState)
from registry import Registry

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


GROUP = "t15o2"
PATH = f"/dev/shm/kraken_{GROUP}"

print("== v2 레이아웃 ==")
shm, mm = kraken_shm.create_shm(GROUP, physical_sm=84, physical_mem_mb=97000)
check("tenant_id 필드 48B (현 최대 17자 ×2 이상)", TENANT_ID_LEN == 48)
check("layout_version 필드 기록", shm.layout_version == SHM_LAYOUT_VERSION == 2)
check("전체 크기 1144B (C sizeof 와 일치는 test_01)",
      ctypes.sizeof(KrakenSharedState) == 1144)

print("== 절단 제거·결정적 해제 (Exp_33 시나리오) ==")
reg = Registry(shm)
i1 = reg.register("keti-gpu0-lsu158", 42, 1000, 1.0)
i2 = reg.register("keti-gpu0-lsu159", 42, 1000, 1.0)
check("16자 ID 무절단 저장",
      shm.alloc[i1].tenant_id.decode() == "keti-gpu0-lsu158")
check("이웃 ID(lsu159) 별도 슬롯·무절단",
      i2 != i1 and shm.alloc[i2].tenant_id.decode() == "keti-gpu0-lsu159")
check("find(159) → 159 슬롯 (과거: 158 슬롯 오매칭)",
      reg.find_slot_by_tenant_id("keti-gpu0-lsu159") == i2)
check("find(158) → 158 슬롯", reg.find_slot_by_tenant_id("keti-gpu0-lsu158") == i1)
reg.deregister(reg.find_slot_by_tenant_id("keti-gpu0-lsu159"))
check("deregister(159) 후 158 은 생존 (결정적 해제)",
      shm.alloc[i1].tenant_id.decode() == "keti-gpu0-lsu158"
      and reg.find_slot_by_tenant_id("keti-gpu0-lsu159") == -1)
i3 = reg.register("keti-gpu15-lsu178", 42, 1000, 1.0)   # 17자 (현 최대)
check("17자 ID (멀티 GPU 상한) 무절단",
      shm.alloc[i3].tenant_id.decode() == "keti-gpu15-lsu178")

print("== 길이 초과 = 명시적 거부 (조용한 절단 금지) ==")
try:
    reg.register("x" * TENANT_ID_LEN, 1, 1, 1.0)
    rejected = False
except ValueError as e:
    rejected = "절단 금지" in str(e)
check("필드 폭 이상 ID register → ValueError + 사유", rejected)
check("초과 ID find → -1 (예외 아님)",
      reg.find_slot_by_tenant_id("x" * TENANT_ID_LEN) == -1)

print("== 구 레이아웃 명시적 거부 ==")
shm.magic = SHM_MAGIC_V1          # v1 SHM 위장
del shm, reg
mm.close()
try:
    shm2, mm2 = kraken_shm.open_shm(GROUP)
    refused = False
except RuntimeError as e:
    refused = "구 레이아웃" in str(e)
check("v1 magic SHM 연결 → RuntimeError (조용한 오독 금지)", refused)
os.unlink(PATH)

n, p = len(results), sum(results)
print(f"\n결과: {p}/{n}")
sys.exit(0 if p == n else 1)
