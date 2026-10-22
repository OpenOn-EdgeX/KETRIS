#!/usr/bin/env python3
"""Test 14: 페어 봉투 정책 (Exp_40) — GPU 불필요, 규칙·가드·feeder Σ 검증."""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
from ratio import decide, decide_pair, POLICIES
from feeder import TimeCreditFeeder

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


PD = [{"name": "A", "r": 0.5, "workload_class": "COMPUTE_BOUND"},
      {"name": "B", "r": 0.5, "workload_class": "MEMORY_BOUND"}]

print("== strict = 현행 규약과 동일 (기본 동작 무변경 입증) ==")
d = decide_pair(PD)   # policy 기본값
check("기본 정책 = strict", d["applied_policy"] == "strict")
ok = True
for q in PD:
    ref = decide(q["r"], q["workload_class"], free_sm_ratio=1.0,
                 free_time_ratio=q["r"])
    m = d["members"][q["name"]]
    if (m["space_ratio"] != ref["space_ratio"]
            or abs(m["time_ratio"] - ref["time_ratio"]) > 1e-9):
        ok = False
check("strict 멤버 출력 = 기존 decide(free_time=r) 완전 일치", ok)
check("strict pd 0.5/0.5 → 양쪽 s=1.0·t=0.5·게이트 (Exp_38 유일해)",
      all(m["space_ratio"] == 1.0 and abs(m["time_ratio"] - 0.5) < 1e-9
          and m["gate"] for m in d["members"].values()))

print("== relaxed_hetero (Exp_39b C 팔) ==")
d = decide_pair(PD, policy="relaxed_hetero")
check("적용 정책 relaxed_hetero·폴백 없음",
      d["feasible"] and d["applied_policy"] == "relaxed_hetero"
      and d["fallback_reason"] is None)
check("COMPUTE=공격자 → t=0.5 게이트 유지",
      d["members"]["A"]["gate"] and abs(d["members"]["A"]["time_ratio"] - 0.5) < 1e-9)
check("MEMORY=피해자 → t=1.0 게이트 해제·s=full(pct99)",
      not d["members"]["B"]["gate"] and d["members"]["B"]["time_ratio"] == 1.0
      and d["members"]["B"]["limit_pct"] == 99)
check("Σt > 1.0 (봉투 완화 명시)",
      sum(m["time_ratio"] for m in d["members"].values()) > 1.0)

print("== capped_hetero (Exp_39b P 팔) ==")
d = decide_pair(PD, policy="capped_hetero", mps_running=True)
check("MPS 기동 시 MEMORY s=r=0.5 (pct50) 상한",
      d["applied_policy"] == "capped_hetero"
      and d["members"]["B"]["space_ratio"] == 0.5
      and d["members"]["B"]["limit_pct"] == 50
      and not d["members"]["B"]["gate"])
check("COMPUTE 는 relaxed 와 동일 (s=full, t=r 게이트)",
      d["members"]["A"]["limit_pct"] == 99 and d["members"]["A"]["gate"])

print("== 폴백 4종 (가드) ==")
d = decide_pair([{"name": "A", "r": 0.5, "workload_class": "COMPUTE_BOUND"},
                 {"name": "B", "r": 0.5, "workload_class": "COMPUTE_BOUND"}],
                policy="relaxed_hetero")
check("① 동종 페어 → strict 폴백 + 사유",
      d["applied_policy"] == "strict" and "이종 혼합 아님" in d["fallback_reason"])
d = decide_pair([{"name": "A", "r": 0.5, "workload_class": "COMPUTE_BOUND"},
                 {"name": "B", "r": 0.5, "workload_class": "MEMORY_BOUND",
                  "confidence": "LOW"}], policy="relaxed_hetero")
check("② 신뢰도 LOW → strict 폴백 (Exp_37 가드)",
      d["applied_policy"] == "strict" and "신뢰도" in d["fallback_reason"])
d = decide_pair([{"name": "A", "r": 0.3, "workload_class": "COMPUTE_BOUND"},
                 {"name": "B", "r": 0.3, "workload_class": "MEMORY_BOUND"},
                 {"name": "C", "r": 0.3, "workload_class": "MIXED"}],
                policy="relaxed_hetero")
check("③ MIXED 포함 3-tenant → strict 폴백 (이종 혼합 아님)",
      d["applied_policy"] == "strict" and "이종 혼합 아님" in d["fallback_reason"])

print("== 3-tenant 이종 혼합 (Exp_58 §3 확장) ==")
T3 = [{"name": "A", "r": 0.33, "workload_class": "COMPUTE_BOUND"},
      {"name": "B", "r": 0.33, "workload_class": "MEMORY_BOUND"},
      {"name": "C", "r": 0.33, "workload_class": "COMPUTE_BOUND"}]
d = decide_pair(T3, policy="relaxed_hetero")
check("C2+M1 relaxed 적용 (실측 OC 0.503→1.021)",
      d["feasible"] and d["applied_policy"] == "relaxed_hetero")
check("  공격자 2 전원 게이트 t=r, 피해자 해제",
      d["members"]["A"]["gate"] and d["members"]["C"]["gate"]
      and not d["members"]["B"]["gate"]
      and d["members"]["B"]["time_ratio"] == 1.0)
T3M = [{"name": "A", "r": 0.33, "workload_class": "COMPUTE_BOUND"},
       {"name": "B", "r": 0.33, "workload_class": "MEMORY_BOUND"},
       {"name": "C", "r": 0.33, "workload_class": "MEMORY_BOUND"}]
d = decide_pair(T3M, policy="relaxed_hetero")
check("C1+M2 relaxed 적용 (실측 OC 0.540→1.325, 피해자 2 동시 해방)",
      d["feasible"] and d["applied_policy"] == "relaxed_hetero"
      and not d["members"]["B"]["gate"] and not d["members"]["C"]["gate"])
d = decide_pair(T3, policy="capped_hetero", mps_running=True)
check("capped 3+ → relaxed 폴백 (미실측 가드)",
      d["applied_policy"] == "relaxed_hetero" and "미실측" in d["fallback_reason"])
d = decide_pair(T3, policy="relaxed_hetero")
check("  3+ 페어링 예측은 보류 유지 (2-tenant 한정)",
      d["pairing_prediction"]["available"] is False)
d = decide_pair(PD, policy="capped_hetero", mps_running=False)
check("④ capped + MPS 부재 → relaxed 폴백 + 경고 (O-3)",
      d["applied_policy"] == "relaxed_hetero" and d["warnings"]
      and "MPS" in d["fallback_reason"])
check("  폴백해도 MEMORY 게이트 해제는 유지 (s 상한만 미적용)",
      d["members"]["B"]["limit_pct"] == 99 and not d["members"]["B"]["gate"])

print("== 입력 검증 ==")
d = decide_pair(PD, policy="nope")
check("알 수 없는 정책 → infeasible + 사유", not d["feasible"]
      and "알 수 없는 정책" in d["fallback_reason"])
d = decide_pair([{"name": "A", "r": 0.7, "workload_class": "COMPUTE_BOUND"},
                 {"name": "B", "r": 0.5, "workload_class": "MEMORY_BOUND"}],
                policy="relaxed_hetero")
check("Σr > 1 → infeasible (계약 몫 위반)", not d["feasible"]
      and "계약 몫 위반" in d["fallback_reason"])

print("== feeder Σ 검증 (Exp_40 Phase 0 확정: strict=기존 정규화 유지+경고) ==")
f = TimeCreditFeeder(send_fn=lambda *a: True, sleep_fn=lambda s: None)
f.register("A", "/tmp/x", "/tmp/x.log", 0.5)
f.register("B", "/tmp/y", "/tmp/y.log", 1.0)   # Σ=1.5 — 기존 동작: 허용
check("strict: Σ=1.5 등록 허용 (하위호환 — test_10 정규화 의미론 유지)",
      f.status()["tenants"].get("B") is not None)
check("strict: Σ>1 등록이 이력에 경고로 기록 (관측 가능성)",
      any("warning" in h and "Σratio" in h.get("warning", "")
          for h in f.status()["recent_resets"]))
f.deregister("B")
f.register("B", "/tmp/y", "/tmp/y.log", 0.5)
warn_n = sum(1 for h in f.status()["recent_resets"] if "warning" in h)
f.register("B", "/tmp/y", "/tmp/y.log", 0.5)   # 재등록 — Σ 계산에 자기 제외
check("재등록(같은 이름 갱신)은 이중계상 없음 — 경고 미발생",
      sum(1 for h in f.status()["recent_resets"] if "warning" in h) == warn_n)
f.set_env_policy("relaxed_hetero", reason="test")
warn_n = sum(1 for h in f.status()["recent_resets"] if "warning" in h)
f.register("C2", "/tmp/z", "/tmp/z.log", 1.0)
check("relaxed: Σ>1 등록 허용 + 경고 없음 (규약상 정상 경로)",
      f.status()["tenants"].get("C2") is not None
      and sum(1 for h in f.status()["recent_resets"]
              if "warning" in h) == warn_n)
check("status 에 env_policy 노출", f.status()["env_policy"] == "relaxed_hetero")
check("정책 변경이 이력에 기록",
      any("env_policy" in h for h in f.status()["recent_resets"]))
try:
    f.set_env_policy("bogus")
    bad = False
except ValueError:
    bad = True
check("알 수 없는 feeder 정책 → ValueError", bad)

print("== budgets in-envelope 불변식 유지 (relaxed 하 armed 집합) ==")
f2 = TimeCreditFeeder(send_fn=lambda *a: True, sleep_fn=lambda s: None)
f2.set_env_policy("relaxed_hetero")
f2.register("G", "/tmp/g", "/tmp/g.log", 0.5)    # 공격자 (게이트)
f2.register("Fr", "/tmp/f", "/tmp/f.log", 1.0)   # 피해자 (미무장 = 해제 경로)
f2.arm("G")
b = f2.budgets()
check("피해자 미무장 → budgets 는 공격자만", list(b) == ["G"])
check("공격자 duty 0.5 절대 해석 (Σarmed≤1)", abs(b["G"] - 5000) <= 1,
      f"budget={b['G']}μs (tick 10ms)")
f2.arm("Fr")   # 방어적: 둘 다 무장돼도 봉투 불변식은 유지되는가
b = f2.budgets()
check("(방어) Σarmed=1.5 무장 시에도 Σbudget ≤ tick 풀",
      sum(b.values()) <= 10000 + 2)

n = len(results)
p = sum(results)
print(f"\n결과: {p}/{n}")
sys.exit(0 if p == n else 1)
