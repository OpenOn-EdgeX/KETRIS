#!/usr/bin/env python3
"""Test 07: 커널 분류 엔진 (Exp_13) — GPU 불필요, 합성 트레이스 검산."""
import os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profiler'))
from kernel_class import classify_kernel_name, parse_trace, summarize, validate_output

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


def kline(dur_ns, name, blocks=100):
    return f"K|1000|{dur_ns}|{blocks},1,1|256,1,1|7|32|0|{name}"


def write_trace(lines):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write("\n".join(lines) + "\n")
    f.close()
    return f.name


print("== 패턴 분류 (근거 명시) ==")
c, basis = classify_kernel_name("ampere_fp16_s1688gemm_fp16_128x128_ldg8_relu_f2f_tn")
check("GEMM(+fused relu epilogue) → COMPUTE", c == "COMPUTE", basis[:40])
c, _ = classify_kernel_name("void gemv2T_kernel_val<int, __half>")
check("GEMV → MEMORY (override: matvec AI)", c == "MEMORY")
c, _ = classify_kernel_name("pytorch_flash::flash_fwd_splitkv_kernel<...>")
check("flash_fwd_splitkv → MEMORY (override 가 flash_fwd 보다 우선)", c == "MEMORY")
c, _ = classify_kernel_name("pytorch_flash::flash_fwd_kernel<...>")
check("flash_fwd(prefill) → COMPUTE", c == "COMPUTE")
c, basis = classify_kernel_name("my_custom_kernel_xyz")
check("미매칭 → UNCERTAIN (임의 라벨 금지)", c == "UNCERTAIN"
      and "분류하지 않음" in basis)

print("== 트레이스 파싱·집계 ==")
tr = write_trace([kline(1000, "foo_gemm", 84), kline(3000, "foo_gemm", 84),
                  "M|1|2|1048576|1", "S|1|2|2048"])
p = parse_trace(tr)
check("K/M/S 레코드 파싱", len(p["kernels"]) == 2
      and p["memcpy_bytes"] == 1048576 and p["memset_bytes"] == 2048)
o = summarize(tr, "t1")
k = o["top_kernels"][0]
check("집계: count/total/avg", k["count"] == 2 and k["avg_us"] == 2.0,
      f"count={k['count']} avg={k['avg_us']}us")
os.unlink(tr)

print("== 분류·힌트·confidence ==")
# decode 시그니처: gemv 지배 (시간 80%)
tr = write_trace([kline(80_000, "gemv2T_kernel_val<__half>"),
                  kline(20_000, "cutlass_80_tensorop_f16_s16816gemm")])
o = summarize(tr, "llm", workload_type="inference")
check("gemv 지배 → MEMORY_BOUND + decode 힌트",
      o["classification"] == "MEMORY_BOUND"
      and o["prefill_decode_hint"] == "decode",
      f"ratio={o['compute_memory_ratio']}")
os.unlink(tr)

# prefill 시그니처: gemm 지배
tr = write_trace([kline(90_000, "cutlass_80_tensorop_f16_s16816gemm"),
                  kline(10_000, "vectorized_elementwise_kernel<4>")])
o_inf = summarize(tr, "llm", workload_type="inference")
o_tr = summarize(tr, "llm", workload_type="training")
check("gemm 지배 추론 → COMPUTE_BOUND + prefill 힌트",
      o_inf["classification"] == "COMPUTE_BOUND"
      and o_inf["prefill_decode_hint"] == "prefill")
check("같은 트레이스라도 training 이면 힌트 n/a (추론 국면 시그니처)",
      o_tr["prefill_decode_hint"] == "n/a")
os.unlink(tr)

# UNCERTAIN 지배 → confidence LOW
tr = write_trace([kline(50_000, "mystery_kernel_a"),
                  kline(30_000, "cutlass_80_gemm"),
                  kline(20_000, "vectorized_elementwise")])
o = summarize(tr, "unk")
check("UNCERTAIN 점유 >0.2 → confidence LOW",
      o["confidence"] == "LOW" and o["uncertain_time_share"] == 0.5,
      f"unc={o['uncertain_time_share']}")
check("ratio 는 분류된 것만으로 계산 (UNCERTAIN 제외)",
      o["compute_memory_ratio"] == 0.6)
os.unlink(tr)

# 빈 트레이스 → sanity 거부
tr = write_trace(["#start_deferred"])
try:
    summarize(tr, "empty")
    check("커널 0개 트레이스 → 거부 (sanity gate)", False)
except ValueError:
    check("커널 0개 트레이스 → 거부 (sanity gate)", True)
os.unlink(tr)

print("== 출력 스키마 ==")
tr = write_trace([kline(1000, "foo_gemm")])
o = summarize(tr, "schema-check")
check("validate_output 통과", validate_output(o) == [])
check("source=kernel_class + 근거(basis) 필드",
      o["source"] == "kernel_class"
      and all("basis" in t for t in o["top_kernels"]))
os.unlink(tr)

n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if n_pass == len(results) else 1)
