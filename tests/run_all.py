#!/usr/bin/env python3
"""전체 통합 테스트 실행"""
import subprocess, sys, os, time

ROOT  = os.path.dirname(__file__)
TESTS = [
    ("01 SHM 레이아웃",     "test_01_shm.py"),
    ("02 Controller API",   "test_02_controller.py"),
    ("03 gating_lib 로드",  "test_03_gating.py"),
    ("04 시간 분할 측정",   "test_04_timesharing.py"),
    ("06 정적 추정 엔진",   "test_06_static_estimator.py"),
    ("07 커널 분류 엔진",   "test_07_kernel_class.py"),
    ("08 시계열 엔진",      "test_08_timeseries.py"),
    ("09 비율 결정 엔진",   "test_09_ratio_engine.py"),
    ("10 폐루프 배선",      "test_10_lifecycle.py"),
    ("11 urgent 선점 배선", "test_11_priority.py"),
    ("12 Turnaround Booster", "test_12_booster.py"),
    ("13 API 명세 정합",      "test_13_api_spec.py"),
    ("14 봉투 정책 (Exp_40)",  "test_14_envelope_policy.py"),
    ("15 O-2 무음 절단 제거 (Exp_41)", "test_15_o2_dereg.py"),
    ("16 NPU 요청 게이트 (Exp_45)",   "test_16_npu_gate.py"),
    ("17 분류엔진 v2 공간지표 (Exp_48)", "test_17_class_v2.py"),
    ("18 페어링 예측 (Exp_49)",        "test_18_pair_predict.py"),
]

PYPATH = ":".join([
    os.path.join(ROOT, '..', 'shm'),
    os.path.join(ROOT, '..', 'controller'),
    os.path.join(ROOT, '..', 'monitor'),
    os.path.join(ROOT, '..', 'profiler'),
])

results = []
for name, fname in TESTS:
    print(f"\n{'='*60}")
    print(f"  Running: {name}")
    print('='*60)
    env = os.environ.copy()
    env["PYTHONPATH"] = PYPATH + ":" + env.get("PYTHONPATH", "")
    t0 = time.monotonic()
    r  = subprocess.run(
        [sys.executable, os.path.join(ROOT, fname)],
        env=env
    )
    elapsed = time.monotonic() - t0
    results.append((name, r.returncode == 0, elapsed))

print(f"\n{'='*60}")
print("  결과 요약")
print('='*60)
all_pass = True
for name, ok, elapsed in results:
    status = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  [{status}] {name:30s}  {elapsed:.1f}s")
    all_pass &= ok

print('='*60)
print(f"  전체: {'PASS' if all_pass else 'FAIL'}")
sys.exit(0 if all_pass else 1)
