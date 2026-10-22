#!/usr/bin/env python3
"""통합 벤치 하니스 (Exp_50 — 11월 벤치 재사용 자산).

시나리오 JSON → 자동 실행 → 한 런에서 3계열 지표 동시 수집:
  · 처리량(테넌트별 sps → OC/유지율은 aggregate.py 가 솔로 대비 산출)
  · GPU 활용률 2정의: nvidia-smi util 1초 샘플 + CUPTI 커널 트레이스(부착 수집,
    유니온/합산 밀도는 aggregate.py) — 정의 차는 report 에 기록
  · 논리 배정률: Σr (LSU 몫 합 — 실행 전 결정값)
  부가: charged(us, time_stats Δ), 메모리 사용 샘플, 타임라인.

정책(시간 축):
  none           게이트 없음 (솔로/자유 실행 대조)
  strict         전원 무장, budget_i = TICK×r_i/max(1,Σr) — feeder budgets() 의미론
  relaxed_hetero cls=C 만 무장(budget=TICK×r_i), cls=M 은 unlimited (Exp_39b C 팔)
공간 축: BLESS_LIMIT_PCT = min(99, round(r×100)) — MPS 하 실효 (O-3, MPS 필수).
메모리: BLESS_MEM_QUOTA_MB = round(r×물리MB) 주입 — ★집행은 할당 경로 의존
  (torch 캐싱 할당자 우회 실측, Exp_50 P0-2c) — 기록용.

시나리오 스키마 (scenarios/*.json):
  {"defaults": {"duration": s, "reps": n},
   "runs": [{"tag", "policy", "members": [{"name","role","batch","r","cls"}]}]}

usage: harness.py --scenario <json> [--reps N] [--only 접두어] [--out results.jsonl]
전제: MPS 기동 상태 (driver 가 관리), GPU1 사용 (env BENCH_GPU_UUID 로 변경 가능).
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

D = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(D, "..")
GPU_UUID = os.environ.get("BENCH_GPU_UUID",
                          "GPU-de67e9b5-fd88-d5b3-133f-8c839735ab87")  # 230 GPU1
GPU_IDX = os.environ.get("BENCH_GPU_IDX", "1")
MPS_PIPE = os.environ.get("CUDA_MPS_PIPE_DIRECTORY", "/tmp/bench-mps")
MPS_LOG = os.environ.get("CUDA_MPS_LOG_DIRECTORY", "/tmp/bench-mps-log")
LIBBLESS = os.environ.get("BENCH_LIBBLESS", os.path.join(ROOT, "runtime", "libbless", "libbless.so"))
TRACER = os.path.join(ROOT, "profiler", "collector", "kernel_tracer.so")
PHYS_MEM_MB = int(os.environ.get("BENCH_PHYS_MEM_MB", "48500"))
TICK_S = 0.010

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ("LD_PRELOAD", "KTRACE_OUT", "KTRACE_DEFER")}


def send(sock_path, msg):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(msg.encode(), sock_path)
        s.close()
    except OSError:
        pass


def read_time_stats(logpath):
    try:
        m = re.findall(r"total=(-?\d+) kernels=(\d+)", open(logpath).read())
        return (int(m[-1][0]), int(m[-1][1])) if m else None
    except OSError:
        return None


class Sampler(threading.Thread):
    """nvidia-smi 1초 샘플러 — util%, memory.used MB."""

    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.stop = [], False

    def run(self):
        while not self.stop:
            out = subprocess.run(
                ["nvidia-smi", "-i", GPU_IDX,
                 "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, env=CLEAN_ENV).stdout.strip()
            try:
                u, m = out.split(",")
                self.rows.append((int(u), int(m)))
            except ValueError:
                pass
            time.sleep(1.0)


def spawn(member, sync, dur, trace_path):
    env = dict(CLEAN_ENV)
    r = float(member["r"])
    pct = min(99, max(1, round(r * 100)))
    env.update({
        "CUDA_VISIBLE_DEVICES": GPU_UUID,
        "CUDA_MPS_PIPE_DIRECTORY": MPS_PIPE,
        "CUDA_MPS_LOG_DIRECTORY": MPS_LOG,
        "WORKER_ROLE": member["role"], "WORKER_NAME": member["name"],
        "BATCH": str(member.get("batch", 1)),
        "SYNC_DIR": sync, "DURATION_S": str(dur),
        "LD_PRELOAD": f"{LIBBLESS}:{TRACER}",
        "LD_LIBRARY_PATH": os.environ.get("BENCH_CUPTI_LIB", "/usr/local/cuda/extras/CUPTI/lib64"),
        "KTRACE_OUT": trace_path,
        "BLESS_TENANT": member["name"],
        "BLESS_MASTER": os.path.join(sync, "no-master.sock"),
        "BLESS_LIMIT_PCT": str(pct),
        "BLESS_MEM_QUOTA_MB": str(round(r * PHYS_MEM_MB)),
    })
    env.update({k: str(v) for k, v in member.get("env", {}).items()})
    logf = open(os.path.join(sync, f"{member['name']}.log"), "w")
    p = subprocess.Popen([sys.executable, os.path.join(D, "worker.py")],
                         env=env, stdout=logf, stderr=logf)
    return p, pct


def run_one(run, dur, rep, trace_dir):
    sync = tempfile.mkdtemp(prefix="bench_sync_")
    members = run["members"]
    procs, pcts, traces = {}, {}, {}
    for m in members:
        tp = os.path.join(trace_dir, f"trace_{run['tag']}_{m['name']}_r{rep}.txt")
        procs[m["name"]], pcts[m["name"]] = spawn(m, sync, dur, tp)
        traces[m["name"]] = tp
    socks = {n: f"/tmp/bless-{p.pid}.sock" for n, p in procs.items()}
    logs = {n: os.path.join(sync, f"{n}.log") for n in procs}

    t0 = time.time()
    while time.time() - t0 < 420:
        if all(os.path.exists(os.path.join(sync, f"ready_{n}")) for n in procs):
            break
        if any(p.poll() is not None for p in procs.values()):
            for n in procs:
                sys.stderr.write(open(logs[n]).read()[-1500:])
            raise RuntimeError(f"워커 조기 종료 ({run['tag']})")
        time.sleep(0.1)
    else:
        raise RuntimeError(f"ready 타임아웃 ({run['tag']})")

    # 시간 축 정책
    policy = run.get("policy", "none")
    rs = {m["name"]: float(m["r"]) for m in members}
    cls = {m["name"]: m.get("cls", "C") for m in members}
    budgets = {}
    if policy == "strict":
        denom = max(1.0, sum(rs.values()))
        budgets = {n: max(1, int(TICK_S * 1e6 * rs[n] / denom)) for n in procs}
        armed = set(procs)
    elif policy == "relaxed_hetero":
        armed = {n for n in procs if cls[n] == "C"}
        budgets = {n: max(1, int(TICK_S * 1e6 * rs[n])) for n in armed}
    else:
        armed = set()

    stop_evt = threading.Event()

    def feed():
        for n in procs:
            if n in armed:
                send(socks[n], "time_mode 1")
                send(socks[n], "time_credit 0")
            else:
                send(socks[n], "time_credit -1")
        while not stop_evt.is_set():
            for n in armed:
                send(socks[n], f"time_add {budgets[n]}")
            time.sleep(TICK_S)

    feeder = threading.Thread(target=feed, daemon=True)
    sampler = Sampler()

    open(os.path.join(sync, "go"), "w").close()
    feeder.start()
    sampler.start()
    time.sleep(0.4)
    for n in procs:
        send(socks[n], "time_stats")
    time.sleep(0.1)
    st0 = {n: read_time_stats(logs[n]) for n in procs}

    # 종료 직전 스냅샷 (워커 생존 중이어야 응답 가능)
    time.sleep(max(0.0, dur - 1.2))
    for n in procs:
        send(socks[n], "time_stats")
    time.sleep(0.2)
    st1 = {n: read_time_stats(logs[n]) for n in procs}
    for p in procs.values():
        p.wait(timeout=dur + 300)
    sampler.stop = True
    stop_evt.set()

    row = {"tag": run["tag"], "rep": rep, "policy": policy,
           "duration": dur, "alloc_rate": round(sum(rs.values()), 4),
           "members": {}}
    for m in members:
        n = m["name"]
        try:
            res = json.load(open(os.path.join(sync, f"result_{n}.json")))
        except FileNotFoundError:
            res = {}
        charged = (st1[n][0] - st0[n][0]) if st0.get(n) and st1.get(n) else None
        # [Exp_63] 공간 노브 실효 판정은 라벨이 아니라 init 로그의 sm_limit 로
        # 한다(Exp_60 측정 함정 3 — 조건이 조용히 FALLBACK 으로 바뀌는 것 감지).
        sm_limit = None
        try:
            mm = re.search(r"sm_limit=([A-Za-z()O0-9 -]+?),", open(logs[n]).read())
            sm_limit = mm.group(1) if mm else None
        except OSError:
            pass
        row["members"][n] = {
            "role": m["role"], "batch": m.get("batch", 1), "r": rs[n],
            "cls": cls[n], "pct": pcts[n], "armed": n in armed,
            "sps": res.get("sps"), "steps": res.get("steps"),
            "charged_us": charged, "trace": traces[n], "sm_limit": sm_limit}
    u = sampler.rows
    row["util_mean"] = round(sum(x for x, _ in u) / len(u), 1) if u else None
    row["util_max"] = max((x for x, _ in u), default=None)
    row["mem_used_max_mb"] = max((m_ for _, m_ in u), default=None)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--only", default=None, help="tag 접두어 필터")
    ap.add_argument("--out", default=None)
    ap.add_argument("--trace-dir", default=None)
    a = ap.parse_args()
    sc = json.load(open(a.scenario))
    dur = sc.get("defaults", {}).get("duration", 15)
    reps = a.reps or sc.get("defaults", {}).get("reps", 3)
    out_path = a.out or os.path.join(D, "results.jsonl")
    trace_dir = a.trace_dir or os.path.join(D, "traces")
    os.makedirs(trace_dir, exist_ok=True)
    outf = open(out_path, "a", buffering=1)
    for run in sc["runs"]:
        if a.only and not run["tag"].startswith(a.only):
            continue
        for rep in range(reps):
            print(f"[bench] {run['tag']} r{rep}", flush=True)
            try:
                row = run_one(run, dur, rep, trace_dir)
            except Exception as e:
                print(f"  FAIL: {e}", flush=True)
                outf.write(json.dumps({"tag": run["tag"], "rep": rep,
                                       "error": str(e)}) + "\n")
                continue
            outf.write(json.dumps(row, ensure_ascii=False) + "\n")
            ms = ", ".join(f"{n}:{v['sps']}" for n, v in row["members"].items())
            print(f"  util {row['util_mean']}% mem {row['mem_used_max_mb']}MB | {ms}",
                  flush=True)


if __name__ == "__main__":
    main()
