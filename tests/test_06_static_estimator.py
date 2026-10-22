#!/usr/bin/env python3
"""Test 06: 정적 프로파일링 추정 엔진 (Exp_12-A) — GPU 불필요, 식 검산."""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profiler'))
from static_estimate import parse_annotations, estimate, validate_output
from static_estimate.schema import SpecError, parse_param_count, DTYPE_BYTES

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond

A = "keti.re.kr/"

print("== Phase 0: annotation 파싱 ==")
check("params 표기 해석", parse_param_count("6.7B") == 6_700_000_000
      and parse_param_count("124M") == 124_000_000)
spec = parse_annotations({A+"model": "llama-7b", A+"workload-type": "inference",
                          A+"dtype": "fp16"})
check("기본값 정책 (batch=1, tp/pp/dp=1, dtype 명시)",
      spec.batch == 1 and spec.tp == 1 and "dtype" not in spec.defaults_used)
check("dtype 누락 → fp32 (보수적)",
      parse_annotations({A+"model": "gpt2", A+"workload-type": "training"}).dtype == "fp32")
try:
    parse_annotations({A+"dtype": "fp16", A+"workload-type": "inference"})
    check("model/params 둘 다 누락 → 거부", False)
except SpecError:
    check("model/params 둘 다 누락 → 거부", True)
try:
    parse_annotations({A+"model": "gpt2"})
    check("workload-type 누락 → 거부", False)
except SpecError:
    check("workload-type 누락 → 거부", True)

print("== Phase 1: 추정 식 검산 ==")
# llama-7b fp16 추론: weights = 6.74e9×2 B ≈ 13.48 GB(decimal) — 공지 값 ~13.5GB
out = estimate({A+"model": "llama-7b", A+"workload-type": "inference",
                A+"dtype": "fp16", A+"batch": "1", A+"seqlen": "2048"})
w_gb_dec = out["breakdown_mb"]["weights"] * 1.048576 / 1000  # MiB→GB(decimal)
check("llama-7b fp16 weights ≈ 13.5GB", abs(w_gb_dec - 13.48) < 0.2,
      f"{w_gb_dec:.2f} GB")
# KV = 2·1·2048·32·4096·2 B = 1 GiB (토큰당 2·H·b·L = 512KB × 2048)
check("llama-7b KV 캐시 = 1024MB", out["breakdown_mb"]["kv_cache"] == 1024,
      f"{out['breakdown_mb']['kv_cache']} MB")
check("추론 → MEMORY_BOUND", out["workload_type"] == "MEMORY_BOUND")
check("추론 confidence HIGH (DB+dtype 명시)", out["confidence"] == "HIGH")

# 학습: 모델 상태 = P×16 (weight 중복 계상 없음)
out_tr = estimate({A+"model": "gpt2", A+"workload-type": "training",
                   A+"dtype": "fp32", A+"batch": "8", A+"seqlen": "1024"})
opt_mb = out_tr["breakdown_mb"]["optimizer_state"]
check("gpt2 학습 모델상태 = 124M×16B", abs(opt_mb - 124e6 * 16 / 2**20) < 2,
      f"{opt_mb} MB")
check("학습 시 weights 항 별도 계상 없음 (P×16 에 포함)",
      out_tr["breakdown_mb"]["weights"] is None)
check("학습 → COMPUTE_BOUND", out_tr["workload_type"] == "COMPUTE_BOUND")
check("gpt2 학습 activation 산출", out_tr["breakdown_mb"]["activation"] > 0,
      f"{out_tr['breakdown_mb']['activation']} MB")

# TP 분할: tp=2 → weights 절반
out_tp = estimate({A+"model": "llama-7b", A+"workload-type": "inference",
                   A+"dtype": "fp16", A+"tp": "2"})
check("tp=2 → weights 절반", abs(out_tp["breakdown_mb"]["weights"] * 2
      - out["breakdown_mb"]["weights"]) <= 2,
      f"{out_tp['breakdown_mb']['weights']} MB")

# 미등록 모델 params 역산 → LOW
out_unk = estimate({A+"params": "13B", A+"workload-type": "inference",
                    A+"dtype": "fp16"})
check("미등록 모델(params 역산) → confidence LOW", out_unk["confidence"] == "LOW")

# CNN: KV 캐시 없음, 학습 activation 미추정
out_cnn = estimate({A+"model": "resnet50", A+"workload-type": "training",
                    A+"dtype": "fp16", A+"batch": "64"})
check("CNN 학습 activation 미추정 표기",
      "activation" in out_cnn["unestimated"])

print("== Phase 2: 출력 스키마 ==")
for o, nm in [(out, "llama-7b 추론"), (out_tr, "gpt2 학습"), (out_unk, "역산")]:
    check(f"validate_output 통과: {nm}", validate_output(o) == [])
check("source=static_estimate + note(보정 대상) 라벨",
      out["source"] == "static_estimate" and "보정 대상" in out["note"])
check("register_mapping tenant_id ≤ 15자",
      len(out["register_mapping"]["tenant_id"]) <= 15)
check("SM 초기값 범위 [1,84]", 1 <= out["sm_init"] <= 84, f"sm={out['sm_init']}")
check("token_budget ≤ round 100ms", 0 < out["token_budget_us"] <= 100_000,
      f"{out['token_budget_us']} us")

# priority 매핑: urgent→2, bg→0, SLO 있는 추론→2
out_pri = estimate({A+"model": "gpt2", A+"workload-type": "batch"})
out_slo = estimate({A+"model": "llama-7b", A+"workload-type": "inference",
                    A+"dtype": "fp16", A+"target-slo": "100ms"})
check("batch→bg(0), SLO 추론→urgent(2)",
      out_pri["priority"] == 0 and out_slo["priority"] == 2)

print("== Phase 3: Exp_12-B 실측 보정 반영 ==")
from static_estimate import formulas
check("context overhead = 실측 330MB (Exp_12-B)",
      formulas.CUDA_CONTEXT_OVERHEAD_MB == 330)
# CNN 학습 활성값: 실측 테이블 (resnet50 fp32, 82.1MB/sample × B)
out_cnn32 = estimate({A+"model": "resnet50", A+"workload-type": "training",
                      A+"dtype": "fp32", A+"batch": "64"})
check("resnet50 fp32 학습 activation = 실측 테이블 82.1×64",
      abs(out_cnn32["breakdown_mb"]["activation"] - 82.1 * 64) < 2,
      f"{out_cnn32['breakdown_mb']['activation']} MB")
check("실측 테이블 외 조합(fp16 학습)은 미추정 유지",
      "activation" in out_cnn["unestimated"])
# quota 마진: est + clamp(0.15×est, 256, 1024)
check("quota = est_mem + 실측 마진 (256~1024MB)",
      out["quota_mb"] == out["est_mem_mb"] + 1024
      and out_cnn32["quota_mb"] == out_cnn32["est_mem_mb"]
      + formulas.quota_margin_mb(out_cnn32["est_mem_mb"]),
      f"quota={out['quota_mb']}, est={out['est_mem_mb']}")

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
