#!/usr/bin/env python3
"""Test 17: 분류엔진 v2 공간 지표 (Exp_48) — GPU 필요(occ_calc 가 장치 속성 조회).

합성 트레이스로 수식 검증:
  · occupancy = 공식 계산기(cuda_occupancy.h) 값 — 손계산 대조 (256thr/32regs/0smem → 1.0)
  · grid_saturation = min(1, blocks/SM수), device_fill = active×wpb/(SM×max_warps)
  · cm_ratio = v1 patterns 재사용, class_device_fill 분해
  · v1 정본 무변경 검증(파일 md5 아님 — v1 출력 경로가 그대로 동작)
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(ROOT, 'profiler'))
sys.path.insert(0, os.path.join(ROOT, 'profiler', 'kernel_class_v2'))

from classify_v2 import classify_v2, parse_trace_v2  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, cond, info=""):
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


# 합성 트레이스: dur 동일 2커널 — gemm(COMPUTE, 84블록 = SM 정확 커버, occ 1.0 launch)
# + elementwise(MEMORY, 42블록 = SM 절반, 같은 launch)
lines = [
    "K|0|1000000|84,1,1|256,1,1|7|32|0|cutlass_80_tensorop_gemm_fake",
    "K|1000000|1000000|42,1,1|256,1,1|7|32|0|elementwise_kernel_fake",
]
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write("\n".join(lines) + "\n")
    trace = f.name

print("== 파서 ==")
ks = parse_trace_v2(trace)
check("K 2건, threads/blocks 보존", len(ks) == 2 and ks[0]["threads"] == 256
      and ks[0]["blocks"] == 84 and ks[1]["blocks"] == 42)

print("== v2 지표 (A6000: 84 SM, 48 warps/SM 기준 손계산 대조) ==")
r = classify_v2(trace, device=0)
nsm, mw = r["device"]["num_sm"], r["device"]["max_warps"]
check("장치 속성 조회 (SM/워프 한계)", nsm > 0 and mw > 0,
      f"{r['device']['name']} sm={nsm} warps={mw}")
# 256thr/32regs/0smem → 워프 8, 한계=워프(48/8=6블록) → occ = 1.0
check("occupancy = 1.0 (공식 계산기, 손계산 일치)", abs(r["sm_occupancy"] - 1.0) < 1e-6)
exp_sat = (min(1, 84 / nsm) + min(1, 42 / nsm)) / 2
check("grid_saturation = 시간가중 min(1,blocks/SM)", abs(r["grid_saturation"] - exp_sat) < 1e-4,
      f"{r['grid_saturation']} vs {exp_sat:.4f}")
exp_fill = (min(84, 6 * nsm) * 8 / (nsm * mw) + min(42, 6 * nsm) * 8 / (nsm * mw)) / 2
check("device_fill = active×wpb/(SM×max_warps)", abs(r["device_fill"] - exp_fill) < 1e-4,
      f"{r['device_fill']} vs {exp_fill:.4f}")
check("cm_ratio = 0.5 (C/M 시간 동일)", abs(r["cm_ratio"] - 0.5) < 1e-6)
check("class_device_fill 분해 (MEMORY < COMPUTE — 블록 절반)",
      r["class_device_fill"]["MEMORY"] < r["class_device_fill"]["COMPUTE"])

print("== v1 정본 경로 무변경 (같은 트레이스로 v1 CLI 동작) ==")
env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, 'profiler'))
out = subprocess.run([sys.executable, "-m", "kernel_class.classify",
                      "--trace", trace, "--logical-unit-id", "t17"],
                     capture_output=True, text=True, env=env)
ok = out.returncode == 0 and '"kernel_class"' in out.stdout
check("v1 classify CLI 정상 (병행 무간섭)", ok)

os.unlink(trace)
n, p = len(results), sum(results)
print(f"\n결과: {p}/{n}")
sys.exit(0 if p == n else 1)
