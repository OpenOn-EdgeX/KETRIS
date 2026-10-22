#!/usr/bin/env python3
"""Test 10: 폐루프 배선 — 상태머신/피더/장부 (Exp_26) — GPU 불필요, mock 검산."""
import sys, os, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
from feeder import TimeCreditFeeder, TICK_S
from lifecycle import (StateMachine, SwapOrchestrator, IllegalTransition,
                       ResidualLedger, NORMAL, GATED, HANDOFF, NORMAL_PRIME,
                       FALLBACK_GATED)
from lifecycle.machine import LEGAL

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


print("== 상태 전이 전수 ==")
ALL = [NORMAL, GATED, HANDOFF, NORMAL_PRIME, FALLBACK_GATED]
ok_legal = ok_illegal = True
for src in ALL:
    for dst in ALL:
        sm = StateMachine(); sm.state = src
        legal = dst in LEGAL.get(src, set())
        try:
            sm.to(dst)
            if not legal: ok_illegal = False
        except IllegalTransition:
            if legal: ok_legal = False
check("합법 전이 전부 허용", ok_legal, f"{sum(len(v) for v in LEGAL.values())}개")
check("불법 전이 전부 거부 (IllegalTransition)", ok_illegal,
      f"{len(ALL)**2 - sum(len(v) for v in LEGAL.values())}개")
sm = StateMachine()
sm.to(GATED); sm.to(HANDOFF); sm.to(NORMAL_PRIME)
check("정상 사이클 NORMAL→GATED→HANDOFF→NORMAL' + 감사 로그 3건",
      sm.state == NORMAL_PRIME and len(sm.audit) == 3)
check("감사 로그에 from/to/t 기록",
      all(k in sm.audit[0] for k in ("from", "to", "t")))

print("== SwapOrchestrator (mock) ==")
calls = []
def mk_orch(sm, ready_after=2, dead=False, timeout=60):
    st = {"polls": 0}
    def ready(h):
        st["polls"] += 1
        return not dead and st["polls"] > ready_after
    return SwapOrchestrator(
        sm, lambda: calls.append("gate_on"), lambda: calls.append("gate_off"),
        lambda: calls.append("spawn") or "H",
        standby_ready=ready, standby_dead=lambda h: dead,
        drain_old=lambda: calls.append("drain"), ready_timeout_s=timeout)

sm = StateMachine(); calls.clear()
final = mk_orch(sm).run(poll_s=0.001)
check("정상 경로 → NORMAL' + 순서 gate_on→spawn→drain→gate_off",
      final == NORMAL_PRIME and calls == ["gate_on", "spawn", "drain", "gate_off"],
      str(calls))
check("  make-before-break: spawn 이 drain 보다 먼저",
      calls.index("spawn") < calls.index("drain"))

sm = StateMachine(); calls.clear()
final = mk_orch(sm, dead=True).run(poll_s=0.001)
check("fail-respawn 폴백 → FALLBACK_GATED", final == FALLBACK_GATED)
check("  폴백 시 게이트 유지 (gate_off 미호출) + 구 테넌트 유지 (drain 미호출)",
      "gate_off" not in calls and "drain" not in calls, str(calls))

sm = StateMachine(); calls.clear()
orch = mk_orch(sm, ready_after=10**9, timeout=0.05)
final = orch.run(poll_s=0.01)
check("ready 타임아웃 → FALLBACK_GATED (reason=respawn_timeout)",
      final == FALLBACK_GATED and orch.result["reason"] == "respawn_timeout")

print("== 피더 분배 수학 (mock socket) ==")
sent = []
f = TimeCreditFeeder(send_fn=lambda sock, msg: sent.append((sock, msg)) or True,
                     sleep_fn=lambda s: None)
f.register("A", "/s/a", "/l/a", 0.5)
f.register("B", "/s/b", "/l/b", 0.5)
f.arm("A"); f.arm("B")
b = f.budgets()
pool = TICK_S * 1e6
check("in-envelope 불변식: Σbudget ≤ TICK×1e6", sum(b.values()) <= pool,
      f"Σ={sum(b.values())} ≤ {pool:.0f}")
check("0.5:0.5 → 균등 분배", b["A"] == b["B"] == int(pool * 0.5))
check("arm 프로토콜 = time_mode 1 + time_credit 0 (Exp_16)",
      ("/s/a", "time_mode 1") in sent and ("/s/a", "time_credit 0") in sent)
f.set_ratios({"A": 0.667, "B": 0.333}, reason="test 2:1")
b = f.budgets()
check("런타임 목표 변경 즉시 반영 (2:1)", abs(b["A"] / b["B"] - 2.0) < 0.05,
      f"A={b['A']} B={b['B']}")
check("  재설정 이력 기록", f.status()["recent_resets"][-1]["reason"] == "test 2:1")
f.set_ratios({"A": 0.5}, reason="단독 duty")
f.release("B")
b = f.budgets()
check("단독 armed + ratio 0.5 → 절대 duty 0.5 (Σ≤1 은 정규화 안 함, Exp_22 gate)",
      b == {"A": int(pool * 0.5)}, f"{b}")
check("release 프로토콜 = time_credit -1 (unlimited)",
      ("/s/b", "time_credit -1") in sent)
f2 = TimeCreditFeeder(send_fn=lambda *a: True, sleep_fn=lambda s: None)
f2.register("X", "/s", "/l", 2.0); f2.register("Y", "/s2", "/l2", 1.0)
f2.arm("X"); f2.arm("Y")
b = f2.budgets()
check("Σratio>1 → 비례 정규화 (2:1) + envelope 준수",
      sum(b.values()) <= pool and abs(b["X"] / b["Y"] - 2.0) < 0.05)

print("== residual 장부 규칙 ==")
led = ResidualLedger()
led.place("A", 1.0, 0.5, gated=True)
led.place("B", 1.0, 0.5, gated=True)
v = led.view()
check("free_time = 1 − Σt (0.5+0.5 → 0)", v["free_time_ratio"] == 0.0)
check("전원 gated → free_sm = 1.0 (공간 중첩 허용 — 시간 직렬화)",
      v["free_sm_ratio"] == 1.0)
check("순간 공간 점유 보고 = max(s of gated)", v["instant_sm_report"] == 1.0)
led.update("A", gated=False, space_ratio=0.7)
v = led.view()
check("ungated s=0.7 → free_sm = 1 − max(ungated s) = 0.3",
      abs(v["free_sm_ratio"] - 0.3) < 1e-9)
led.remove("A"); led.remove("B")
check("전부 회수 → free (1.0, 1.0)",
      led.view()["free_time_ratio"] == 1.0 and led.view()["free_sm_ratio"] == 1.0)

print("== 회피 권고 = 거부 아님 (v1) ==")
rec_avoid = {"key": "PREFILL+PREFILL", "known": True,
             "recommend": {"sm_split": None, "note": "co-location 회피"}}
w = led.place("P2", 1.0, 0.5, gated=True, pair_recommendation=rec_avoid)
check("회피 권고 → 배치는 수행 + warnings 반환",
      len(w) == 1 and "회피 권고" in w[0] and "P2" in led.view()["tenants"])
check("  감사 로그에 warnings 기록",
      led.audit[-1]["warnings"] and "Exp_29" in led.audit[-1]["warnings"][0])
rec_ok = {"key": "PREFILL+DECODE", "known": True,
          "recommend": {"sm_split": "70/30 (compute 우대)"}}
w = led.place("D1", 0.5, 0.3, gated=True, pair_recommendation=rec_ok)
check("정상 권고 조합 → warnings 없음", w == [])

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
