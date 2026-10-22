#!/usr/bin/env python3
"""Test 09: 비율 결정 엔진 v1 (Exp_25) — GPU 불필요, 규칙 검산."""
import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
from ratio import decide, decide_for_device, bless_limit_pct
from ratio.engine import GRID

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


CATALOG = json.load(open(os.path.join(os.path.dirname(__file__),
                                      '..', 'lsu', 'catalog.json')))

print("== 곡선 수학: s×t ≥ r 불변식 ==")
ok = True
worst = None
for r10 in range(1, 11):
    r = r10 / 10
    for cls in ("COMPUTE_BOUND", "MEMORY_BOUND", "MIXED"):
        d = decide(r, cls)
        if not d["feasible"] or d["space_ratio"] * d["time_ratio"] < r - 1e-9:
            ok = False
            worst = (r, cls, d)
check("모든 r∈{0.1..1.0}×3분류 에서 feasible 하고 s×t ≥ r", ok,
      f"위반: {worst}" if worst else "30조합 전부")

print("== 경계 ==")
d = decide(1.0, "COMPUTE_BOUND")
check("r=1.0 → s=1.0, t=1.0 (유일해)", d["feasible"]
      and d["space_ratio"] == 1.0 and d["time_ratio"] == 1.0)
d = decide(0.01, "MEMORY_BOUND")
check("r→0 (0.01) → 최소 격자 s=0.1", d["feasible"] and d["space_ratio"] == 0.1,
      f"s={d['space_ratio']}, t={d['time_ratio']}")
d = decide(1.5, "COMPUTE_BOUND")
check("r>1 → INFEASIBLE (예외 아님, 구조체)", d["feasible"] is False
      and "범위 밖" in d["reason"])
d = decide(0, "COMPUTE_BOUND")
check("r=0 → INFEASIBLE", d["feasible"] is False)

print("== residual 클리핑 ==")
d = decide(0.5, "COMPUTE_BOUND", free_sm_ratio=0.7)
check("COMPUTE r=0.5, free_sm=0.7 → s=0.7 로 클리핑", d["feasible"]
      and d["space_ratio"] == 0.7, f"s={d['space_ratio']}")
check("  클리핑 사실이 rule_applied 에 기록", "클리핑" in d["rule_applied"],
      d["rule_applied"])
d = decide(0.5, "MEMORY_BOUND", free_time_ratio=0.5)
check("MEMORY r=0.5, free_time=0.5 → t≤0.5 위해 s=1.0 강제", d["feasible"]
      and d["space_ratio"] == 1.0 and abs(d["time_ratio"] - 0.5) < 1e-9)

print("== INFEASIBLE 경로 ==")
d = decide(0.5, "COMPUTE_BOUND", free_sm_ratio=0.4)
check("r=0.5 > free_sm=0.4 → INFEASIBLE + 사유", d["feasible"] is False
      and "생존 후보 0개" in d["reason"], d["reason"])
d = decide(0.5, "MEMORY_BOUND", free_sm_ratio=0.6, free_time_ratio=0.5)
check("s≤0.6 이면 t≥0.83 > free_time=0.5 → INFEASIBLE",
      d["feasible"] is False)
check("  candidates_evaluated=0 기록", d["candidates_evaluated"] == 0)

print("== 선호 규칙 ==")
dc = decide(0.3, "COMPUTE_BOUND")
dm = decide(0.3, "MEMORY_BOUND")
check("동일 r=0.3: COMPUTE 의 s > MEMORY 의 s",
      dc["space_ratio"] > dm["space_ratio"],
      f"C={dc['space_ratio']} vs M={dm['space_ratio']}")
check("  COMPUTE → 격자 최대 s=1.0", dc["space_ratio"] == 1.0)
check("  MEMORY → 격자 최소 feasible s=0.3", dm["space_ratio"] == 0.3)
dn = decide(0.3, "MIXED")
import math
tgt = math.sqrt(0.3)
check("MIXED → 중립 √r 근방", abs(dn["space_ratio"] - tgt) <= 0.05 + 1e-9,
      f"s={dn['space_ratio']}, √r={tgt:.3f}")
dl = decide(0.3, "COMPUTE_BOUND", confidence="LOW")
check("저신뢰(LOW) → COMPUTE 라도 중립 강등", dl["space_ratio"] == dn["space_ratio"],
      f"s={dl['space_ratio']}")

print("== best-fit (동률 해소) ==")
# √0.25=0.5 정중앙 — 0.5 가 격자에 있어 동률 없음. 동률 케이스: √r 이 격자
# 중간(예: r=0.3025, √r=0.55)이면 0.5/0.6 동률 → 잔여 면적 최소 선택.
d = decide(0.3025, "MIXED", free_sm_ratio=0.6, free_time_ratio=1.0)
# 후보 0.5→t=0.605, 0.6→t=0.504: 잔여 (0.6-0.5)×(1-0.605)=0.0395 vs (0.6-0.6)×…=0
check("동률 √r=0.55: 잔여 면적 최소(=0) 후보 s=0.6 선택",
      d["space_ratio"] == 0.6, f"s={d['space_ratio']}, rule={d['rule_applied']}")
check("  best-fit 적용이 rule_applied 에 기록", "best-fit" in d["rule_applied"])

print("== provisional 전파 (catalog) ==")
# [Exp_161] 픽스처를 라이브 catalog.json 과 분리 — 두 번째 재발이라 구조를 고친다.
#   1차: Warboy 가 Exp_46 에서 measured=true 로 승격 → A100 으로 교체(구판 주석)
#   2차: Exp_111 카탈로그 전면 교체(Qwen 기준)로 A100/A6000 항목 자체가 소멸
#        → measured=false 항목이 라이브에 없어 FAIL. 제품 로직
#        (engine.py decide_for_device: provisional = not measured)은 불변 — 낡은 테스트.
#   테스트 의도(측정 여부 → provisional 전파)는 라이브 카탈로그 내용과 무관하므로
#   전용 픽스처로 검산한다. "catalog 밖" 검사만 라이브 CATALOG 를 계속 쓴다.
FIXTURE = {"devices": {
    "미실측GPU": {"lsu": 100, "measured": False},
    "실측GPU":   {"lsu": 65,  "measured": True},
}}
d = decide_for_device(50, "미실측GPU", FIXTURE, "MEMORY_BOUND")
check("measured=false → provisional=True", d["feasible"]
      and d["provisional"] is True, f"r={50/100}, s={d['space_ratio']}")
d = decide_for_device(32, "실측GPU", FIXTURE, "COMPUTE_BOUND")
check("measured=true → provisional=False", d["feasible"]
      and d["provisional"] is False, f"r={32/65:.3f}")
d = decide_for_device(10, "존재안함GPU", CATALOG, "MIXED")
check("catalog 밖 디바이스 → INFEASIBLE (default_lsu 추정 안 함)",
      d["feasible"] is False and "catalog 에 없는" in d["reason"])

print("== 결정성 ==")
runs = [decide(0.37, "COMPUTE_BOUND", 0.8, 0.9) for _ in range(50)]
check("같은 입력 50회 → 전부 동일 출력",
      all(r == runs[0] for r in runs))

print("== 적용 헬퍼 (BLESS_LIMIT_PCT) ==")
check("full s=1.0 → 99 (100 은 libbless getenv 가드 배제 — 암묵 50% 회귀 방지)",
      bless_limit_pct(1.0) == 99)
check("s=0.5 → 50", bless_limit_pct(0.5) == 50)
check("s=0.004 → 하한 1 (0 이면 가드 무효)", bless_limit_pct(0.004) == 1)

print("== 결과 구조체 규약 ==")
d = decide(0.5, "COMPUTE_BOUND")
need = ("space_axis", "time_axis", "candidates_evaluated", "rule_applied",
        "provisional", "reason", "warnings")
check("필수 필드 전부 존재", all(k in d for k in need))
check("비대칭 축 명시 (space=spawn-time / time=runtime)",
      "spawn-time" in d["space_axis"] and "runtime" in d["time_axis"])
check("launch_ceiling 경고 자리 예약 (v1 미검사 → None)",
      d["warnings"]["launch_ceiling"] is None)

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
