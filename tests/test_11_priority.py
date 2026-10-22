#!/usr/bin/env python3
"""Test 11: urgent 선점 배선 (Exp_27) — GPU 불필요, 소켓 mock 검산."""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
from priority import PriorityManager, PRIORITY_CLASSES

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


SOCKS = {"A": "/s/a", "B": "/s/b", "C": "/s/c"}
sent = []
pm = PriorityManager(send_fn=lambda sock, msg: sent.append((sock, msg)) or True,
                     sock_resolver=lambda t: SOCKS.get(t))

print("== 등급 어휘 (libbless.cpp:701-708 소스 확인 프로토콜) ==")
check("어휘 = URGENT:0 / NORMAL:1 / BACKGROUND:2",
      PRIORITY_CLASSES == {"URGENT": 0, "NORMAL": 1, "BACKGROUND": 2})

print("== 명령 문자열 형식 ==")
sent.clear(); pm.set_priority("A", "URGENT")
check("priority_set 형식 = 'priority_set 0'", sent == [("/s/a", "priority_set 0")],
      str(sent))
sent.clear(); pm.set_priority("B", "BACKGROUND")
check("BACKGROUND → 'priority_set 2'", sent == [("/s/b", "priority_set 2")])

print("== urgent 팬아웃 의미론 (플래그는 프로세스-로컬 — 소스 근거) ==")
sent.clear()
out = pm.urgent("A", ["A", "B", "C"])
check("urgent(A): A 에 priority_set 0 + B,C 에 urgent_request",
      ("/s/a", "priority_set 0") in sent
      and ("/s/b", "urgent_request") in sent
      and ("/s/c", "urgent_request") in sent, str(sent))
check("  urgent 대상 자신에게는 urgent_request 미발신",
      ("/s/a", "urgent_request") not in sent)
check("  pending_sent 반환 = [B, C]", sorted(out) == ["B", "C"])
check("  상태 유지 (urgent_holder=A)", pm.status()["urgent_holder"] == "A")

sent.clear()
cleared = pm.urgent_clear()
check("urgent_clear: 보냈던 B,C 전원에 urgent_clear 발신",
      sorted(sent) == [("/s/b", "urgent_clear"), ("/s/c", "urgent_clear")],
      str(sent))
check("  해제 후 상태 초기화", pm.status()["urgent_holder"] is None
      and pm.status()["pending_sent"] == [] and sorted(cleared) == ["B", "C"])

print("== 거부 경로 ==")
try:
    pm.set_priority("A", "HIGH")
    check("잘못된 등급(HIGH — scheduler.py 구 어휘) 거부", False)
except ValueError as e:
    check("잘못된 등급(HIGH — scheduler.py 구 어휘) 거부", "URGENT" in str(e))
try:
    pm.set_priority("Z", "NORMAL")
    check("미등록 테넌트 거부", False)
except KeyError as e:
    check("미등록 테넌트 거부", "feeder" in str(e))
sent.clear()
try:
    pm.urgent("Z", ["A"])
except KeyError:
    pass
check("  미등록 urgent 대상 — 발신 0건 (부분 발신 없음)", sent == [])

print("== 감사 이력 ==")
st = pm.status()
kinds = [a["kind"] for a in st["audit"]]
check("등급 변경·urgent·clear 전부 이력 기록",
      "priority_set" in kinds and "urgent" in kinds and "urgent_clear" in kinds)
check("이력에 시각(t) 포함", all("t" in a for a in st["audit"]))

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
