#!/usr/bin/env python3
"""Test 02: Controller HTTP API (subprocess 기동 후 테스트)"""
import sys, os, time, subprocess, urllib.request, json, signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shm'))
from kraken_shm import open_shm, MODE_FREE, MODE_OVERCOMMIT

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
ROOT = os.path.join(os.path.dirname(__file__), '..')
CTRL = os.path.join(ROOT, 'controller', 'kraken_controller.py')
PORT = 18090   # 충돌 방지용 포트
URL  = f"http://localhost:{PORT}"

def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    return cond

def call(path, data=None):
    url = URL + path
    if data is not None:
        body = json.dumps(data).encode()
        req  = urllib.request.Request(url, data=body,
               headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())

all_ok = True
proc = None

print("=== Test 02: Controller 기동 ===")
try:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(ROOT, "shm") + ":" + os.path.join(ROOT, "controller")
    proc = subprocess.Popen(
        [sys.executable, CTRL,
         "--group", "test02", "--port", str(PORT), "--no-mps"],
        env=env, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    )

    # 기동 대기 (최대 5초)
    for _ in range(50):
        try:
            call("/status")
            break
        except Exception:
            time.sleep(0.1)
    else:
        all_ok = False
        print(f"  [{FAIL}] Controller 기동 실패 (5초 타임아웃)")
        sys.exit(1)

    print("  Controller 기동 완료")

    print("\n=== Test 02: /status ===")
    s = call("/status")
    all_ok &= check("mode == FREE",     s["mode"] == "FREE")
    all_ok &= check("physical_sm > 0",  s["physical_sm"] > 0,  f"sm={s['physical_sm']}")
    all_ok &= check("tenant_count == 0", s["tenant_count"] == 0)

    # [Exp_161] virtual_sm 을 물리 SM 상대값으로 — 구판은 42×3=126 하드코딩이었다.
    #   구 230 A6000(물리 84 SM, 주석 "84<=84"가 흔적) 전제라 232 Pro6000(188 SM)
    #   에서는 126 ≤ 188 → FREE 가 제품 규칙(overcommit.py: total > physical 만
    #   OVERCOMMIT)대로 맞는 동작인데 테스트가 FAIL — 낡은 테스트(환경 의존 상수).
    #   물리값을 /status 로 읽어 절반씩 등록: 2×(phys//2) ≤ phys → FREE,
    #   3×(phys//2) > phys (phys≥2) → OVERCOMMIT. 환경이 바뀌어도 성립.
    phys = s["physical_sm"]
    half = phys // 2

    print("\n=== Test 02: /register (A, B) — FREE 모드 유지 ===")
    r = call("/register", {"tenant_id": "A", "virtual_sm": half, "virtual_mem_mb": 4096})
    all_ok &= check("A registered", "tenant_idx" in r, str(r))
    idx_a = r.get("tenant_idx", -1)
    all_ok &= check("A slot 0",  idx_a == 0, f"slot={idx_a}")
    all_ok &= check("A mps_pct set", r["env"]["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] != "0")

    r = call("/register", {"tenant_id": "B", "virtual_sm": half, "virtual_mem_mb": 4096})
    idx_b = r.get("tenant_idx", -1)
    all_ok &= check("B registered", idx_b == 1, f"slot={idx_b}")

    s = call("/status")
    all_ok &= check(f"virtual_sm_total == {2*half}", s["virtual_sm_total"] == 2*half,
                    f"total={s['virtual_sm_total']}")
    all_ok &= check(f"mode still FREE ({2*half}<={phys})", s["mode"] == "FREE", s["mode"])

    print("\n=== Test 02: /register (C) — OVERCOMMIT 전환 ===")
    r = call("/register", {"tenant_id": "C", "virtual_sm": half, "virtual_mem_mb": 4096})
    idx_c = r.get("tenant_idx", -1)
    all_ok &= check("C registered", idx_c == 2, f"slot={idx_c}")

    s = call("/status")
    all_ok &= check(f"virtual_sm_total == {3*half}", s["virtual_sm_total"] == 3*half)
    all_ok &= check(f"mode == OVERCOMMIT ({3*half}>{phys})",
                    s["mode"] == "OVERCOMMIT", s["mode"])

    print("\n=== Test 02: /deregister (C) — FREE 복귀 ===")
    call("/deregister", {"tenant_idx": idx_c})
    s = call("/status")
    all_ok &= check(f"virtual_sm_total == {2*half}", s["virtual_sm_total"] == 2*half)
    all_ok &= check("mode == FREE", s["mode"] == "FREE", s["mode"])

    print("\n=== Test 02: /policy/weights ===")
    call("/policy/weights", {"weights": [0.6, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
    s = call("/status")
    all_ok &= check("policy version bumped", s["policy_version"] >= 1)

    print("\n=== Test 02: /tenants ===")
    t = call("/tenants")
    names = [x["tenant_id"] for x in t["tenants"]]
    all_ok &= check("A, B in tenants", "A" in names and "B" in names, str(names))

    print("\n=== Test 02: /stats ===")
    st = call("/stats")
    all_ok &= check("2 tenants in stats", len(st["stats"]) == 2,
                    f"count={len(st['stats'])}")

finally:
    if proc:
        proc.terminate()
        proc.wait(timeout=3)
    shm_path = "/dev/shm/kraken_test02"
    if os.path.exists(shm_path):
        os.unlink(shm_path)

print(f"\n{'전체 통과' if all_ok else '실패 있음'}")
sys.exit(0 if all_ok else 1)
