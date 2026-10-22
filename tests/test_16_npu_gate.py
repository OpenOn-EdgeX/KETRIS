#!/usr/bin/env python3
"""Test 16: NPU 요청 게이트 코어 + feeder NPU 타입 (Exp_45) — GPU/NPU 하드웨어 불필요.

runtime/npu_proxy/gate_core.py 를 가짜 runner·가짜 시계로 검증:
  · deficit 선택으로 7:3 비례 실현 (feeder 주입 예산 기준)
  · ★Exp_44 오답 회귀 2건 — max-credit 선택 금지 / strict(작업보존 폴백 없음)
  · time_credit -1(unlimited)·상한 클램프·time_stats 형식(feeder 정규식 호환)
  · feeder register(resource=) 태그 보존 (GPU 기본값 하위호환)
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(ROOT, 'runtime', 'npu_proxy'))
sys.path.insert(0, os.path.join(ROOT, 'controller'))

from gate_core import GateCore, CREDIT_CAP_US
from feeder.feeder import TimeCreditFeeder, read_time_stats

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, cond, info=""):
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


# ── 가짜 실행 환경: 요청 1건 = 1400us, 시계는 실행 시에만 전진 ───────────────
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


RUN_US = 1400


def make_core():
    clk = FakeClock()

    def run_fn(req):
        clk.t += RUN_US / 1e6

    return GateCore(run_fn, clock=clk), clk


def fill(core, name, n=10_000):
    for _ in range(n):
        core.submit(name, {})


print("== deficit 비례 (7:3 예산 주입) ==")
core, clk = make_core()
for t in ("A", "B"):
    core.register(t)
    core.cmd(t, "time_mode", "1")
    core.cmd(t, "time_credit", "0")
fill(core, "A")
fill(core, "B")
# feeder tick 모사: 10ms 마다 7:3 예산, 총 3초분
for _ in range(300):
    core.cmd("A", "time_add", str(int(10_000 * 0.7)))
    core.cmd("B", "time_add", str(int(10_000 * 0.3)))
    while core.step():
        st = core.stats()
        if st["A"]["credit_us"] <= 0 and st["B"]["credit_us"] <= 0:
            break
st = core.stats()
share_a = st["A"]["charged_us"] / (st["A"]["charged_us"] + st["B"]["charged_us"])
check("charged 점유 A ≈ 0.7", abs(share_a - 0.7) < 0.02, f"실측 {share_a:.4f}")

print("== ★오답 회귀 1: max-credit 선택 금지 ==")
core, clk = make_core()
for t in ("A", "B"):
    core.register(t)
    core.cmd(t, "time_mode", "1")
fill(core, "A", 5)
fill(core, "B", 5)
# A: credit 크지만 이미 많이 씀(정규화 deficit 큼) / B: credit 작지만 굶음
core.cmd("A", "time_add", "50000")
core.cmd("B", "time_add", "50000")
for _ in range(3):          # A 만 3건 실행된 상태를 만든다
    core._tenants["A"].charged_us += RUN_US
name, _, _ = core.step()
check("credit 최대(A)가 아니라 deficit 최소(B) 선택", name == "B")

print("== ★오답 회귀 2: strict(work_conserving=False) — credit 소진 시 유휴(보존 옵션) ==")
# [Exp_85] non-work-conserving 은 이제 옵션(회귀/대조용). 기본은 work-conserving.
clk2 = FakeClock()
core = GateCore(lambda r: None, clock=clk2, work_conserving=False)
core.register("A")
core.cmd("A", "time_mode", "1")
core.cmd("A", "time_credit", "0")
fill(core, "A", 5)
check("[non-WC] armed+credit0 → step()=None (실행 안 함)", core.step() is None)
core.cmd("A", "time_add", "2000")
check("[non-WC] time_add 후 실행 재개", core.step() is not None)

print("== ★Exp_85: work-conserving(기본) — credit 소진해도 일 있으면 실행 ==")
core, clk = make_core()          # work_conserving=True 기본
core.register("A")
core.cmd("A", "time_mode", "1")
core.cmd("A", "time_credit", "0")
fill(core, "A", 5)
check("[WC] armed+credit0 → step() 실행(유휴 안 함)", core.step() is not None)
# work-conserving 이 deficit 순서는 유지: 크레딧 있는 쪽 우선
core2, _ = make_core()
for t in ("A", "B"):
    core2.register(t); core2.cmd(t, "time_mode", "1"); core2.cmd(t, "time_credit", "0")
fill(core2, "A", 5); fill(core2, "B", 5)
core2.cmd("B", "time_add", "50000")   # B 만 크레딧 → B 우선(A 는 credit0 후순위)
name, _, _ = core2.step()
check("[WC] 크레딧 있는 B 가 credit0 A 보다 우선", name == "B")

print("== unlimited·클램프·회계 ==")
core, clk = make_core()
core.register("A")
core.cmd("A", "time_mode", "1")
core.cmd("A", "time_credit", "-1")
fill(core, "A", 3)
check("time_credit -1 → unlimited 실행", core.step() is not None)
core.cmd("A", "time_add", str(10 * CREDIT_CAP_US))
check("credit 상한 클램프", core.stats()["A"]["credit_us"] <= CREDIT_CAP_US)
r = core.step()
check("charged 실측 누적", core.stats()["A"]["charged_us"] >= 2 * RUN_US - 1)

print("== time_stats 형식 = feeder 정규식 호환 ==")
resp = core.cmd("A", "time_stats", None)
m = re.search(r"total=(-?\d+) kernels=(\d+)", resp)
check("'total=<us> kernels=<n>' 매치", m is not None, resp)
check("kernels = 완료 수", m and int(m.group(2)) == core.stats()["A"]["done"])

print("== unregister → 미처리 요청 반환 ==")
core, clk = make_core()
core.register("A")
fill(core, "A", 4)
pend = core.unregister("A")
check("대기 4건 반환 + 테넌트 소거", len(pend) == 4 and "A" not in core.tenants())

print("== feeder resource 태그 (Exp_45) ==")
f = TimeCreditFeeder(send_fn=lambda *a: True, sleep_fn=lambda s: None)
f.register("G", "/tmp/g.sock", "/tmp/g.log", 0.5)
f.register("N", "/tmp/n.sock", "/tmp/n.log", 0.5, resource="npu")
st = f.status()["tenants"]
check("기본값 gpu (하위호환)", st["G"]["resource"] == "gpu")
check("npu 태그 보존", st["N"]["resource"] == "npu")
f.arm("N")
check("npu 테넌트 budgets 동일 경로", "N" in f.budgets())

n, p = len(results), sum(results)
print(f"\n결과: {p}/{n}")
sys.exit(0 if p == n else 1)
