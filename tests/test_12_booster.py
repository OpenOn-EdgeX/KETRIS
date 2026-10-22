#!/usr/bin/env python3
"""Test 12: Turnaround Booster (Exp_28) — GPU 불필요, 이벤트/소켓 mock 검산."""
import sys, os, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profiler'))
from booster import Booster
from lifecycle import ResidualLedger
from timeseries.schema import build_term_event, validate_term_event

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


class MockFeeder:
    def __init__(self): self.calls = []
    def release(self, t): self.calls.append(("release", t))
    def deregister(self, t): self.calls.append(("deregister", t))


class MockSub:
    def __init__(self, path, handler): self.handler = handler
    def start(self): pass
    def stop(self): pass


def mk(dead_pid=999999999):
    """dead_pid: /proc 에 없는 pid (사망 시뮬레이션)."""
    f, led = MockFeeder(), ResidualLedger()
    led.place("W", 0.5, 0.5, gated=True)
    calls = {"decide": []}
    def decide(r, cls, **kw):
        calls["decide"].append((r, cls, kw))
        return {"feasible": True, "space_ratio": 0.5, "time_ratio": 0.4,
                "rule_applied": "mock"}
    b = Booster(f, led, decide, subscriber_factory=MockSub)
    b.register_tenant("W", dead_pid, "0")
    return b, f, led, calls


def term_ev(gpu="0", confidence="HIGH", basis="vram_release", pids=None):
    ev = build_term_event(gpu=gpu, pids=pids or [], evidence={
        "basis": basis, "confidence": confidence, "t_signal": 1.0,
        "hold_s": 0.0, "baseline_mb": 15, "peak_mb": 2000,
        "util": 0, "mem_mb": 15, "periodicity": None})
    return ev


print("== 이벤트 스키마 소비 (validate_term_event) ==")
ev = term_ev()
check("정본 스키마 이벤트 생성·검증 통과", validate_term_event(ev) == [])
bad = dict(ev); bad["evidence"] = {**ev["evidence"], "confidence": "MAYBE"}
check("스키마 위반(confidence enum) 검출", validate_term_event(bad) != [])

print("== 파이프라인 순서 ==")
b, f, led, calls = mk()
b.enable("event", events_path="/dev/null")
b.add_pending(0.3, "MEMORY_BOUND")
b._on_event(term_ev())
check("액션 1건 완료", len(b.actions) == 1)
a = b.actions[0]
order = ["t_detect", "t_recheck", "t_feeder_done", "t_ledger_done",
         "t_dereg_done", "t_realloc_ready"]
check("단계 타임스탬프 전부 기록 + 단조 증가",
      all(k in a for k in order) and
      all(a[order[i]] <= a[order[i+1]] for i in range(len(order)-1)))
check("feeder 정리 = release→deregister",
      f.calls == [("release", "W"), ("deregister", "W")])
check("ledger 자원 반환 (W 제거)", "W" not in led.view()["tenants"])
check("재할당 준비: 대기 요청에 decide 호출 + 결과 기록",
      len(calls["decide"]) == 1 and a["realloc_decision"]["feasible"])
check("  decide 입력 = 반환 후 ledger view (free_time=1.0)",
      calls["decide"][0][2]["free_time_ratio"] == 1.0)

print("== 오탐 방어 ==")
b, f, led, calls = mk(dead_pid=os.getpid())     # 살아있는 pid
b.enable("event", events_path="/dev/null")
b._on_event(term_ev())
check("생존 재확인: alive → 액션 중단 (해지 없음)",
      len(b.actions) == 0 and f.calls == []
      and "W" in led.view()["tenants"])
check("  중단 사유 감사 기록", any(x["kind"] == "action_aborted_alive"
                                   for x in b.audit))
b, f, led, calls = mk()
b.enable("event", events_path="/dev/null")
b._on_event(term_ev(confidence="LOW", basis="idle_hold"))
check("LOW confidence(idle_hold) → 액션 안 함 (관측만)",
      len(b.actions) == 0 and any(x["kind"] == "skip_low_confidence"
                                  for x in b.audit))
b, f, led, calls = mk()
b.enable("event", events_path="/dev/null")
b._on_event(term_ev(gpu="7"))
check("gpu 불일치 → 매칭 실패 skip", len(b.actions) == 0
      and any(x["kind"] == "skip_no_match" for x in b.audit))

print("== 토글 ==")
b, f, led, calls = mk()
b.enable("event", events_path="/dev/null")
b.disable()
b._on_event(term_ev())
check("disable 후 이벤트 무시", len(b.actions) == 0)
b.enable("event", events_path="/dev/null")
b._on_event(term_ev())
check("재enable 후 동작", len(b.actions) == 1)
try:
    b.enable("magic")
    check("잘못된 mode 거부", False)
except ValueError:
    check("잘못된 mode 거부", True)

print("== poll 모드 (OFF 기준선) — 동일 파이프라인 ==")
b, f, led, calls = mk()
b._run_pipeline("W", time.time(), trigger="poll_1s")
check("poll 트리거도 동일 단계 기록", len(b.actions) == 1
      and b.actions[0]["trigger"] == "poll_1s"
      and all(k in b.actions[0] for k in order))

print("== 실구독 경로 (EventSubscriber match — 1차 측정에서 잡힌 결함 회귀 감시) ==")
import json as _json, tempfile as _tf
from lifecycle import EventSubscriber
_evf = os.path.join(_tf.mkdtemp(prefix="t12_"), "ev.jsonl")
got = []
sub = EventSubscriber(_evf, lambda e: got.append(e),
                      match=lambda e: e.get("event") == "TERM_PREDICTED")
sub.start()
with open(_evf, "w") as fh:
    fh.write(_json.dumps(term_ev()) + "\n")
t_end = time.time() + 2
while not got and time.time() < t_end:
    time.sleep(0.02)
sub.stop()
check("match=TERM_PREDICTED 구독: 파일 append → 핸들러 호출", len(got) == 1)
got2 = []
sub2 = EventSubscriber(_evf, lambda e: got2.append(e))   # 기본 match
sub2.start(); time.sleep(0.3); sub2.stop()
check("기본 match(transition)는 TERM 이벤트 무시 (Exp_26 하위호환)", got2 == [])

print("== 감사 로그 ==")
st = b.status()
check("status: enabled/mode/tenants/actions/audit 노출",
      all(k in st for k in ("enabled", "mode", "tenants", "actions", "audit")))
check("audit 에 등록·액션 이력", any(x["kind"] == "tenant_registered"
                                     for x in st["audit"]))

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
