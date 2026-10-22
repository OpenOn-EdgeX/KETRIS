#!/usr/bin/env python3
"""Test 08: 시계열 엔진 4-3/4-4 (Exp_14) — GPU 불필요, 합성 시계열 검산."""
import os, sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profiler'))
from timeseries import (bin_events, detect_period, detect_period_scan,
                        mem_peak_analysis, detect_termination,
                        build_output, validate_output)
from timeseries.termination import idle_gaps, NAIVE_HOLD_S

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


rng = np.random.default_rng(14)

print("== 4-3 주기 검출 ==")
# 주기 0.5s: iteration 마다 커널 버스트(200개, 0.2s 폭) + 잡음
events = []
for k in range(30):
    events.extend(k * 0.5 + rng.uniform(0, 0.2, 200))
x, _ = bin_events(sorted(events), dt=0.005)
p = detect_period(x, dt=0.005, max_lag_s=5.0)
check("주기 0.5s 버스트 → 검출 (오차 ≤5%)",
      p["periodic"] and abs(p["period_s"] - 0.5) / 0.5 <= 0.05,
      f"period={p['period_s']} strength={p['strength']}")
check("FFT 교차 확인 일치", p["fft_agrees"] is True,
      f"fft={p['fft_period_s']}")

# 비주기: Poisson 도착 버스트 — periodic=False 가 정답 (억지 주기 금지)
arrivals = np.cumsum(rng.exponential(0.5, 40))
events = [t + d for t in arrivals for d in rng.uniform(0, 0.05, 50)]
x, _ = bin_events(sorted(events), dt=0.005)
p_ap = detect_period(x, dt=0.005, max_lag_s=5.0)
check("Poisson 버스트 → periodic=False (억지 주기 금지)",
      not p_ap["periodic"] and p_ap["period_s"] is None,
      f"strength={p_ap['strength']}")
check("비주기여도 근거(basis)·strength 보고", "basis" in p_ap
      and isinstance(p_ap["strength"], float))

print("== 4-3 후보 스캔 (primary 우선 + 합의) ==")
# 전체 스트림이 평탄(균일 파이프라인)해도 시그니처 커널로 주기 검출 (decode 사례)
uniform = sorted(rng.uniform(0, 10, 5000))            # 'all' 평탄(비주기 밀집)
jit = lambda n, p: np.arange(n) * p + rng.normal(0, 0.0005, n)  # 실측 유사 지터
sig_a = sorted(jit(385, 0.026))                       # step 당 1회 커널 다수
sig_b = sorted(jit(385, 0.026) + 0.001)
sig_2x = sorted(jit(192, 0.052))                      # 격 step 발화(2×주기) 소수
sc = detect_period_scan({"all": uniform, "sig:a": sig_a,
                         "sig:b": sig_b, "sig:2x": sig_2x}, dt=0.002)
check("평탄한 전체 + 시그니처 합의 → 미세 주기 검출 (2× 소수는 걸러짐)",
      sc["periodic"] and abs(sc["period_s"] - 0.026) <= 0.004
      and sc.get("consensus_n", 0) >= 2,
      f"period={sc['period_s']} series={sc['series']} n={sc.get('consensus_n')}")
# 전체 스트림이 주기면 서브 시계열(더 강한 미세 주기)보다 우선
burst_all = []
for k in range(20):
    burst_all.extend(k * 0.5 + rng.uniform(0, 0.2, 300))
fine = list(np.arange(0, 10, 0.02))                   # 강한 20ms 서브 리듬
sc2 = detect_period_scan({"all": sorted(burst_all), "sig:fine": fine}, dt=0.002)
check("primary('all') 주기 우선 — 서브 미세주기에 안 끌려감",
      sc2["periodic"] and abs(sc2["period_s"] - 0.5) / 0.5 <= 0.05
      and sc2["series"] == "all",
      f"period={sc2['period_s']} series={sc2['series']}")

print("== 4-3 메모리 피크 예측 ==")
# 주기 0.5s, 톱니(활성값 누적→해제), 피크 진폭 1000MB
t = np.arange(0, 10, 0.01)
saw = 1000.0 * ((t % 0.5) / 0.5)
mp = mem_peak_analysis(t, 2000 + saw, 0.5)
check("주기 창별 피크 반복 검출", mp is not None and mp["n_peaks"] >= 15,
      f"n_peaks={mp['n_peaks']}" if mp else "None")
check("피크 크기 안정(CV<5%) + 예측 오차 <50ms",
      mp["peak_mb_cv"] < 0.05 and mp["pred_err_ms_median"] < 50,
      f"cv={mp['peak_mb_cv']} err={mp['pred_err_ms_median']}ms")
check("주기 미검출 시 피크 예측 안 함 (None)",
      mem_peak_analysis(t, 2000 + saw, None) is None)

print("== 4-4 종료 감지 ==")
# util 시계열 (100ms): 활동 90~100%, iteration 휴지 0.8s, 마지막에 진짜 종료
dt = 0.1
seg = []
for k in range(8):                      # 8 iteration: 1.2s 활동 + 0.8s 휴지
    seg += [95] * 12 + [0] * 8
seg += [0] * 100                        # 진짜 종료 (10s 유휴)
tt = np.arange(len(seg)) * dt
t_last = 8 * 2.0 - 0.8                  # 마지막 활동 종료 시각
ev_naive = detect_termination(tt, seg, hold_s=NAIVE_HOLD_S)
fp_naive = [e for e in ev_naive if e["t_signal"] < t_last]
check("naive(0.5s hold): iteration 휴지마다 오탐", len(fp_naive) >= 7,
      f"FP={len(fp_naive)}")
ev_aware = detect_termination(tt, seg, period_s=2.0)   # 4-3 주기 결합
fp_aware = [e for e in ev_aware if e["t_signal"] < t_last]
tp_aware = [e for e in ev_aware if e["t_signal"] >= t_last]
check("주기 결합(hold=3×주기): 오탐 0 + 진짜 종료 정탐",
      len(fp_aware) == 0 and len(tp_aware) == 1,
      f"FP={len(fp_aware)} lat={tp_aware[0]['t_signal']-t_last:.1f}s" if tp_aware else "미검출")
check("주기 결합 신호 confidence=HIGH + 근거 명시",
      tp_aware and tp_aware[0]["confidence"] == "HIGH"
      and "주기" in tp_aware[0]["basis"])

# 비주기 → gap 이력 기반
act = tt < t_last
gaps = idle_gaps(tt[act], np.array(seg)[act])
ev_hist = detect_termination(tt, seg, idle_gaps_s=gaps)
fp_hist = [e for e in ev_hist if e["t_signal"] < t_last]
check("gap 이력 결합: 오탐 0 + 정탐", len(fp_hist) == 0
      and any(e["t_signal"] >= t_last for e in ev_hist),
      f"hold={ev_hist[-1]['hold_s']}s" if ev_hist else "미검출")

# 활동 시작 전 유휴(모델 로드)는 종료로 오인하지 않음 (무장 후 감지)
seg_load = [0] * 80 + [95] * 30 + [0] * 60          # 8s 로드 → 3s 활동 → 종료
tt2 = np.arange(len(seg_load)) * dt
ev_load = detect_termination(tt2, seg_load, hold_s=1.0)
check("활동 전 유휴(로드 구간)에서 발화 안 함 (무장 로직)",
      all(e["t_signal"] >= 11.0 for e in ev_load) and len(ev_load) == 1,
      f"events={[round(e['t_signal'],1) for e in ev_load]}")

# VRAM 해제 신호: util-hold 보다 빠른 확정 신호 + teardown util 스파이크 무관
mem = [16000] * 110 + [18] * 60                      # 활동 후 즉시 해제
ev_mem = detect_termination(tt2, seg_load, hold_s=5.0, mem_used_mb=mem)
mem_evs = [e for e in ev_mem if e["kind"] == "mem_release"]
check("VRAM 해제 → hold 대기 없이 즉시 발화 (kind=mem_release)",
      len(mem_evs) == 1 and mem_evs[0]["t_signal"] < 11.0 + 5.0
      and mem_evs[0]["confidence"] == "HIGH",
      f"t={mem_evs[0]['t_signal']:.1f}s" if mem_evs else "미발화")

print("== 출력 스키마 ==")
o = build_output("t1", p, mp, ev_aware, est_remaining_s=1.5)
check("validate_output 통과", validate_output(o) == [])
check("source=timeseries + 종료 신호 필드",
      o["source"] == "timeseries" and o["termination"]["terminating"] is True)
bad = dict(o); bad["periodicity"] = {**o["periodicity"],
                                     "periodic": False}   # period_s 는 남긴 채
check("periodic=False 인데 period_s 있으면 거부 (억지 주기 금지)",
      validate_output(bad) != [])

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
