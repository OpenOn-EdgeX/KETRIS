#!/usr/bin/env python3
"""
KRAKEN 시공간 공유 시각화 — 실측 데이터 기반
==============================================
4개 시나리오를 실제로 실행하고 측정하여 그래프 저장:

  (a) 시간 분할  2-way : LLM + GEMM          [OVERCOMMIT, 1 SHM]
  (b) 시간 분할  3-way : LLM + GEMM_A + GEMM_B [OVERCOMMIT, 1 SHM]
  (c) 시공간 분할 2+2  : P1(LLM+GEMM) | P2(GEMM_A+GEMM_B)  [MPS=50% each]
  (d) 시공간 분할 2+3  : P1(LLM+GEMM_A+GEMM_B) | P2(GEMM_C+GEMM_D+GEMM_E)

사용:
  cd kraken/tests
  python3 visualize_st.py [--iters N] [--sp-iters N] [--interval S]
"""
import sys, os, time, subprocess, threading
from pathlib import Path

ROOT  = Path(__file__).parent.parent
TESTS = Path(__file__).parent
LIB   = ROOT / "runtime" / "gating_lib.so"
WL_G  = TESTS / "workload_gemm.py"
WL_L  = TESTS / "workload_llm.py"
OUT   = TESTS / "results"
OUT.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT / "shm"))
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "monitor"))

from kraken_shm import create_shm, MODE_OVERCOMMIT, MODE_FREE, GATE_RUNNING
from registry  import Registry
from metrics_logger import MetricsLogger


def preinit_timeslices(shm, n_tenants):
    """레이스 컨디션 방지: 워크로드 시작 전 time_remain 사전 충전."""
    for i in range(n_tenants):
        w = shm.policy.weights[i]
        total_w = sum(shm.policy.weights[j] for j in range(n_tenants)) or 1.0
        slice_us = int(shm.policy.round_duration_us * w / total_w)
        shm.tenants[i].time_remain_us = slice_us
        shm.tenants[i].gate_state = GATE_RUNNING

# ── matplotlib / 한글 폰트 ───────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import numpy as np

_KO_FONT = None
_font_path = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"
if os.path.exists(_font_path):
    fm.fontManager.addfont(_font_path)
    _KO_FONT = fm.FontProperties(fname=_font_path).get_name()

plt.rcParams.update({
    "figure.dpi": 150,
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "legend.fontsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    **({"font.family": _KO_FONT} if _KO_FONT else {}),
})
matplotlib.rcParams["axes.unicode_minus"] = False

# ── 색상 ────────────────────────────────────────────────────────────────────
C = {
    "LLM":    "#E74C3C",   # 빨강
    "GEMM":   "#3498DB",   # 파랑
    "GEMM_A": "#27AE60",   # 초록
    "GEMM_B": "#F39C12",   # 주황
    "GEMM_C": "#8E44AD",   # 보라
    "GEMM_D": "#16A085",   # 청록
    "GEMM_E": "#E67E22",   # 다크 주황
    "idle":   "#EEEEEE",
}

PHYSICAL_SM = 84   # A6000 SM 수


# ─────────────────────────────────────────────────────────────────────────────
#  유틸
# ─────────────────────────────────────────────────────────────────────────────
def shm_cleanup(*paths):
    for p in paths:
        if os.path.exists(p):
            os.unlink(p)

def make_env(extra: dict) -> dict:
    e = os.environ.copy()
    e["LD_PRELOAD"] = str(LIB)
    e.update(extra)
    return e

def make_logger(shm, mm, interval):
    lg = MetricsLogger.__new__(MetricsLogger)
    lg.shm = shm; lg._mm = mm
    lg.interval = interval
    lg.stdout = False; lg.output = "/dev/null"
    lg._prev = {}
    return lg


# ─────────────────────────────────────────────────────────────────────────────
#  시나리오 (a): 시간 분할 2-way  [LLM + GEMM]
# ─────────────────────────────────────────────────────────────────────────────
def collect_ts2(iters_llm=80, iters_gemm=500, size=2048,
                interval=0.5, warmup=3.0):
    """
    OVERCOMMIT 2-tenant: LLM(weight=0.5) + GEMM(weight=0.5)
    반환: timeline list[dict]
    """
    SHM = "vis_ts2"
    PATH = f"/dev/shm/kraken_{SHM}"
    shm_cleanup(PATH)

    shm, mm = create_shm(SHM, physical_sm=PHYSICAL_SM, physical_mem_mb=49140)
    reg = Registry(shm)
    reg.register("LLM",  virtual_sm=PHYSICAL_SM, virtual_mem_mb=8192, weight=1.0)
    reg.register("GEMM", virtual_sm=PHYSICAL_SM, virtual_mem_mb=8192, weight=1.0)
    shm.policy.weights[0] = 0.5
    shm.policy.weights[1] = 0.5
    shm.policy.version += 1
    preinit_timeslices(shm, 2)

    print(f"\n[ts2] mode={shm.mode}  MPS: LLM={shm.alloc[0].mps_pct}%  GEMM={shm.alloc[1].mps_pct}%")

    e_llm  = make_env({"KRAKEN_TENANT": "LLM",  "KRAKEN_GROUP": SHM,
                        "WORKLOAD_ITERS": str(iters_llm),
                        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[0].mps_pct)})
    e_gemm = make_env({"KRAKEN_TENANT": "GEMM", "KRAKEN_GROUP": SHM,
                        "WORKLOAD_ITERS": str(iters_gemm), "WORKLOAD_SIZE": str(size),
                        "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[1].mps_pct)})

    p_llm  = subprocess.Popen([sys.executable, str(WL_L)], env=e_llm,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p_gemm = subprocess.Popen([sys.executable, str(WL_G)], env=e_gemm,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    lg = make_logger(shm, mm, interval)
    timeline = []
    t0 = time.monotonic()

    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup)
    lg.snapshot()

    print("  측정 중…")
    while p_llm.poll() is None or p_gemm.poll() is None:
        rows = lg.snapshot()
        ts   = round(time.monotonic() - t0 - warmup, 2)
        if rows and ts > 0:
            d = {r["tenant"]: r for r in rows}
            if "LLM" in d and "GEMM" in d:
                timeline.append({
                    "ts": ts,
                    "LLM_exec":  d["LLM"]["exec_rate_pct"],
                    "GEMM_exec": d["GEMM"]["exec_rate_pct"],
                    "LLM_wait":  d["LLM"]["wait_rate_pct"],
                    "GEMM_wait": d["GEMM"]["wait_rate_pct"],
                    "LLM_fair":  d["LLM"]["fairness"],
                    "GEMM_fair": d["GEMM"]["fairness"],
                })
                print(f"    t={ts:5.1f}s  LLM={d['LLM']['exec_rate_pct']:5.1f}%  "
                      f"GEMM={d['GEMM']['exec_rate_pct']:5.1f}%")
        time.sleep(interval)

    p_llm.wait(timeout=10); p_gemm.wait(timeout=10)
    shm_cleanup(PATH)
    print(f"  완료. 샘플={len(timeline)}")
    return timeline


# ─────────────────────────────────────────────────────────────────────────────
#  시나리오 (b): 시간 분할 3-way  [LLM + GEMM_A + GEMM_B]
# ─────────────────────────────────────────────────────────────────────────────
def collect_ts3(iters_llm=80, iters_gemm=500, size=2048,
                interval=0.5, warmup=3.0):
    SHM = "vis_ts3"
    PATH = f"/dev/shm/kraken_{SHM}"
    shm_cleanup(PATH)

    shm, mm = create_shm(SHM, physical_sm=PHYSICAL_SM, physical_mem_mb=49140)
    reg = Registry(shm)
    w = 1.0 / 3.0
    reg.register("LLM",    virtual_sm=PHYSICAL_SM, virtual_mem_mb=8192, weight=1.0)
    reg.register("GEMM_A", virtual_sm=PHYSICAL_SM, virtual_mem_mb=8192, weight=1.0)
    reg.register("GEMM_B", virtual_sm=PHYSICAL_SM, virtual_mem_mb=8192, weight=1.0)
    for i in range(3):
        shm.policy.weights[i] = w
    shm.policy.version += 1
    preinit_timeslices(shm, 3)

    print(f"\n[ts3] mode={shm.mode}  "
          f"MPS: LLM={shm.alloc[0].mps_pct}%  "
          f"GEMM_A={shm.alloc[1].mps_pct}%  GEMM_B={shm.alloc[2].mps_pct}%")

    mps_llm  = shm.alloc[0].mps_pct
    mps_ga   = shm.alloc[1].mps_pct
    mps_gb   = shm.alloc[2].mps_pct

    e_llm = make_env({"KRAKEN_TENANT": "LLM",    "KRAKEN_GROUP": SHM,
                       "WORKLOAD_ITERS": str(iters_llm),
                       "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_llm)})
    e_ga  = make_env({"KRAKEN_TENANT": "GEMM_A", "KRAKEN_GROUP": SHM,
                       "WORKLOAD_ITERS": str(iters_gemm), "WORKLOAD_SIZE": str(size),
                       "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_ga)})
    e_gb  = make_env({"KRAKEN_TENANT": "GEMM_B", "KRAKEN_GROUP": SHM,
                       "WORKLOAD_ITERS": str(iters_gemm), "WORKLOAD_SIZE": str(size),
                       "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_gb)})

    p_llm = subprocess.Popen([sys.executable, str(WL_L)], env=e_llm,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p_ga  = subprocess.Popen([sys.executable, str(WL_G)], env=e_ga,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p_gb  = subprocess.Popen([sys.executable, str(WL_G)], env=e_gb,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    lg = make_logger(shm, mm, interval)
    timeline = []
    t0 = time.monotonic()

    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup)
    lg.snapshot()

    print("  측정 중…")
    procs = [p_llm, p_ga, p_gb]
    while any(p.poll() is None for p in procs):
        rows = lg.snapshot()
        ts   = round(time.monotonic() - t0 - warmup, 2)
        if rows and ts > 0:
            d = {r["tenant"]: r for r in rows}
            keys = ["LLM", "GEMM_A", "GEMM_B"]
            if all(k in d for k in keys):
                timeline.append({
                    "ts": ts,
                    **{f"{k}_exec": d[k]["exec_rate_pct"] for k in keys},
                    **{f"{k}_wait": d[k]["wait_rate_pct"] for k in keys},
                    **{f"{k}_fair": d[k]["fairness"]      for k in keys},
                })
                row_str = "  ".join(f"{k}={d[k]['exec_rate_pct']:5.1f}%" for k in keys)
                print(f"    t={ts:5.1f}s  {row_str}")
        time.sleep(interval)

    for p in procs: p.wait(timeout=10)
    shm_cleanup(PATH)
    print(f"  완료. 샘플={len(timeline)}")
    return timeline


# ─────────────────────────────────────────────────────────────────────────────
#  시나리오 (c,d): 시공간 분할 (2 파티션 × N 워크로드)
# ─────────────────────────────────────────────────────────────────────────────
def collect_spatial_temporal(n_per_partition: int,
                              iters_llm=80, iters_gemm=400, size=2048,
                              interval=0.5, warmup=3.0):
    """
    n_per_partition = 2 or 3
    P1 (SM 0~50%):  LLM + GEMM_A [+ GEMM_B]
    P2 (SM 50~100%): GEMM_C + GEMM_D [+ GEMM_E]

    각 파티션은 독립 SHM 그룹, physical_sm=42 (50% of 84)
    모든 테넌트 MPS=50%  (CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=50)
    """
    assert n_per_partition in (2, 3)
    tag = f"st{n_per_partition}"

    SHM1 = f"vis_{tag}_p1"
    SHM2 = f"vis_{tag}_p2"
    P1   = f"/dev/shm/kraken_{SHM1}"
    P2   = f"/dev/shm/kraken_{SHM2}"
    shm_cleanup(P1, P2)

    PART_SM  = PHYSICAL_SM // 2   # 42
    MPS_PCT  = 50

    # ── Partition 1 SHM ──────────────────────────────────────────────────────
    shm1, mm1 = create_shm(SHM1, physical_sm=PART_SM, physical_mem_mb=24000)
    reg1 = Registry(shm1)
    if n_per_partition == 2:
        p1_tenants = ["LLM", "GEMM_A"]
        w1 = [0.5, 0.5]
    else:
        p1_tenants = ["LLM", "GEMM_A", "GEMM_B"]
        w1 = [1/3, 1/3, 1/3]
    for t, w in zip(p1_tenants, w1):
        reg1.register(t, virtual_sm=PART_SM, virtual_mem_mb=7000, weight=w*len(p1_tenants))
    for i, w in enumerate(w1):
        shm1.policy.weights[i] = w
    shm1.policy.version += 1
    preinit_timeslices(shm1, len(p1_tenants))

    # ── Partition 2 SHM ──────────────────────────────────────────────────────
    shm2, mm2 = create_shm(SHM2, physical_sm=PART_SM, physical_mem_mb=24000)
    reg2 = Registry(shm2)
    if n_per_partition == 2:
        p2_tenants = ["GEMM_C", "GEMM_D"]
        w2 = [0.5, 0.5]
    else:
        p2_tenants = ["GEMM_C", "GEMM_D", "GEMM_E"]
        w2 = [1/3, 1/3, 1/3]
    for t, w in zip(p2_tenants, w2):
        reg2.register(t, virtual_sm=PART_SM, virtual_mem_mb=7000, weight=w*len(p2_tenants))
    for i, w in enumerate(w2):
        shm2.policy.weights[i] = w
    shm2.policy.version += 1
    preinit_timeslices(shm2, len(p2_tenants))

    print(f"\n[{tag}] P1 mode={shm1.mode}  P2 mode={shm2.mode}  MPS={MPS_PCT}%/partition")

    def mk_env_g(tenant, grp, iters):
        return make_env({"KRAKEN_TENANT": tenant, "KRAKEN_GROUP": grp,
                          "WORKLOAD_ITERS": str(iters), "WORKLOAD_SIZE": str(size),
                          "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(MPS_PCT)})
    def mk_env_l(tenant, grp, iters):
        return make_env({"KRAKEN_TENANT": tenant, "KRAKEN_GROUP": grp,
                          "WORKLOAD_ITERS": str(iters),
                          "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(MPS_PCT)})

    # P1 프로세스
    procs_p1 = []
    for t in p1_tenants:
        wl  = WL_L if t == "LLM" else WL_G
        env = mk_env_l(t, SHM1, iters_llm) if t == "LLM" else mk_env_g(t, SHM1, iters_gemm)
        procs_p1.append(subprocess.Popen([sys.executable, str(wl)], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    # P2 프로세스
    procs_p2 = []
    for t in p2_tenants:
        env = mk_env_g(t, SHM2, iters_gemm)
        procs_p2.append(subprocess.Popen([sys.executable, str(WL_G)], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    all_procs = procs_p1 + procs_p2

    # ── 두 SHM을 동시에 폴링 (스레드) ─────────────────────────────────────────
    lg1 = make_logger(shm1, mm1, interval)
    lg2 = make_logger(shm2, mm2, interval)

    timeline = []
    t_start  = time.monotonic()
    lock     = threading.Lock()

    # 현재 측정값을 저장할 공유 버퍼
    _buf1 = {}   # tenant → row
    _buf2 = {}

    def poll_p1():
        time.sleep(warmup)
        lg1.snapshot()   # prev 초기화
        while any(p.poll() is None for p in all_procs):
            rows = lg1.snapshot()
            with lock:
                for r in (rows or []):
                    _buf1[r["tenant"]] = r
            time.sleep(interval)

    def poll_p2():
        time.sleep(warmup)
        lg2.snapshot()
        while any(p.poll() is None for p in all_procs):
            rows = lg2.snapshot()
            with lock:
                for r in (rows or []):
                    _buf2[r["tenant"]] = r
            time.sleep(interval)

    th1 = threading.Thread(target=poll_p1, daemon=True)
    th2 = threading.Thread(target=poll_p2, daemon=True)
    th1.start(); th2.start()

    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup + interval)  # 첫 샘플 대기

    print("  측정 중…")
    while any(p.poll() is None for p in all_procs):
        ts = round(time.monotonic() - t_start - warmup, 2)
        with lock:
            b1 = dict(_buf1)
            b2 = dict(_buf2)
        if ts > 0 and b1 and b2:
            row = {"ts": ts}
            for t in p1_tenants:
                row[f"{t}_exec"] = b1.get(t, {}).get("exec_rate_pct", 0.0)
            for t in p2_tenants:
                row[f"{t}_exec"] = b2.get(t, {}).get("exec_rate_pct", 0.0)
            timeline.append(row)

            p1_str = "  ".join(f"{t}={row[f'{t}_exec']:5.1f}%" for t in p1_tenants)
            p2_str = "  ".join(f"{t}={row[f'{t}_exec']:5.1f}%" for t in p2_tenants)
            print(f"    t={ts:5.1f}s  P1[{p1_str}]  P2[{p2_str}]")
        time.sleep(interval)

    for p in all_procs: p.wait(timeout=10)
    shm_cleanup(P1, P2)
    print(f"  완료. 샘플={len(timeline)}")
    return timeline, p1_tenants, p2_tenants


# ─────────────────────────────────────────────────────────────────────────────
#  Plot (a),(b): 시간 분할 타임라인
# ─────────────────────────────────────────────────────────────────────────────
def plot_temporal(timeline, tenants, title, fname):
    if not timeline:
        print(f"[{fname}] 데이터 없음"); return

    ts = np.array([d["ts"] for d in timeline])

    fig, axes = plt.subplots(len(tenants) + 1, 1,
                              figsize=(11, 3.2 * (len(tenants) + 1)),
                              sharex=True,
                              gridspec_kw={"height_ratios": [3]*len(tenants) + [2]})
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.01)

    fairness_data = {}
    for i, t in enumerate(tenants):
        ax = axes[i]
        exec_vals = np.array([d.get(f"{t}_exec", 0) for d in timeline])
        wait_vals = np.array([d.get(f"{t}_wait", 0) for d in timeline])
        fair_vals = np.array([d.get(f"{t}_fair", 1) for d in timeline])
        fairness_data[t] = fair_vals

        target = 100.0 / len(tenants)
        wl_label = "LLM (GPT-2 style)" if t == "LLM" else t.replace("_", " ")

        ax.fill_between(ts, exec_vals, alpha=0.75, color=C.get(t, "#888"),
                        label="exec")
        ax.fill_between(ts, wait_vals, alpha=0.35, color="#BBBBBB", label="wait")
        ax.axhline(target, ls="--", lw=1.3, color=C.get(t, "#888"), alpha=0.65,
                   label=f"목표 {target:.0f}%")

        ax.set_ylabel("비율 (%)")
        ax.set_ylim(0, 115)
        ax.set_title(f"테넌트  {wl_label}  (weight={1/len(tenants):.2f})", loc="left")
        ax.legend(loc="upper right", framealpha=0.7)
        ax.grid(axis="y", alpha=0.2)

    # fairness subplot
    ax_f = axes[-1]
    for t in tenants:
        ax_f.plot(ts, fairness_data[t], lw=1.8, color=C.get(t, "#888"),
                  label=t.replace("_", " "))
    ax_f.axhline(1.0, ls="--", lw=1.2, color="#555", alpha=0.6, label="완벽(1.0)")
    ax_f.set_ylabel("Fairness")
    ax_f.set_xlabel("경과 시간 (s)")
    ax_f.set_ylim(0, 2.5)
    ax_f.set_title("Fairness  (실제/목표, 1.0=이상)", loc="left")
    ax_f.legend(loc="upper right", framealpha=0.7, ncol=len(tenants))
    ax_f.grid(axis="y", alpha=0.2)

    plt.tight_layout(rect=[0, 0, 1, 0.99])
    path = OUT / fname
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] 저장: {path}")


# ─────────────────────────────────────────────────────────────────────────────
#  Plot (c),(d): 시공간 분할 — SM 공간×시간 다이어그램
# ─────────────────────────────────────────────────────────────────────────────
def plot_spatial_temporal(timeline, p1_tenants, p2_tenants, title, fname):
    """
    Y축: SM 공간 (0~100%)  X축: 시간
    P1 = 0~50%,  P2 = 50~100%
    각 구간에서 테넌트별 exec_rate에 비례하여 SM 공간을 나눔
    """
    if not timeline:
        print(f"[{fname}] 데이터 없음"); return

    ts = np.array([d["ts"] for d in timeline])
    N  = len(ts)
    dt = float(np.mean(np.diff(ts))) if N > 1 else 0.5

    fig, (ax_main, ax_fair) = plt.subplots(
        2, 1, figsize=(13, 8),
        gridspec_kw={"height_ratios": [4, 1.5]},
        sharex=True
    )
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.01)

    P1_LO, P1_HI = 0,  50
    P2_LO, P2_HI = 50, 100

    # ── SM 공간×시간 다이어그램 ──────────────────────────────────────────────
    legend_patches = []

    for i, row in enumerate(timeline):
        x_lo = ts[i] - dt * 0.45
        x_hi = ts[i] + dt * 0.45

        # Partition 1
        p1_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p1_tenants])
        p1_total = p1_execs.sum() or 1.0
        p1_fracs = p1_execs / p1_total     # 상대 비율 (합=1)
        p1_idle  = max(0.0, 1.0 - p1_execs.sum() / 100.0)   # 미사용 비율

        cursor = P1_LO
        # 실제로 사용된 SM만큼만 채우기
        total_used_p1 = min(1.0, p1_execs.sum() / 100.0)
        for j, t in enumerate(p1_tenants):
            h = p1_fracs[j] * total_used_p1 * (P1_HI - P1_LO)
            if h > 0.1:
                ax_main.fill_between([x_lo, x_hi], [cursor, cursor],
                                     [cursor + h, cursor + h],
                                     color=C.get(t, "#888"), alpha=0.85, linewidth=0)
            cursor += h
        # idle 영역
        if cursor < P1_HI:
            ax_main.fill_between([x_lo, x_hi], [cursor, cursor],
                                 [P1_HI, P1_HI],
                                 color=C["idle"], alpha=0.6, linewidth=0)

        # Partition 2
        p2_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p2_tenants])
        p2_total = p2_execs.sum() or 1.0
        p2_fracs = p2_execs / p2_total
        total_used_p2 = min(1.0, p2_execs.sum() / 100.0)

        cursor = P2_LO
        for j, t in enumerate(p2_tenants):
            h = p2_fracs[j] * total_used_p2 * (P2_HI - P2_LO)
            if h > 0.1:
                ax_main.fill_between([x_lo, x_hi], [cursor, cursor],
                                     [cursor + h, cursor + h],
                                     color=C.get(t, "#888"), alpha=0.85, linewidth=0)
            cursor += h
        if cursor < P2_HI:
            ax_main.fill_between([x_lo, x_hi], [cursor, cursor],
                                 [P2_HI, P2_HI],
                                 color=C["idle"], alpha=0.6, linewidth=0)

    # 파티션 경계
    ax_main.axhline(P1_HI, color="black", lw=2.0, ls="-", zorder=5)
    ax_main.axhline(P2_LO, color="black", lw=2.0, ls="-", zorder=5)
    ax_main.fill_betweenx([P1_HI, P2_LO], ts[0] - dt, ts[-1] + dt,
                           color="white", alpha=1.0, zorder=4)
    ax_main.text((ts[0] + ts[-1]) / 2, (P1_HI + P2_LO) / 2,
                  "── SM 파티션 경계 (MPS=50%) ──",
                  ha="center", va="center", fontsize=8.5, color="#444", zorder=6)

    ax_main.set_ylim(0, 100)
    ax_main.set_ylabel("SM 공간 (%)")
    ax_main.set_yticks([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    ax_main.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_main.grid(axis="y", alpha=0.2)

    # 파티션 라벨
    ax_main.text(-0.08, 0.25, f"Partition 1\n(SM 0~50%)\n{len(p1_tenants)}개 테넌트",
                  transform=ax_main.transAxes, ha="center", va="center",
                  fontsize=8.5, color="#C0392B", fontweight="bold", rotation=90)
    ax_main.text(-0.08, 0.75, f"Partition 2\n(SM 50~100%)\n{len(p2_tenants)}개 테넌트",
                  transform=ax_main.transAxes, ha="center", va="center",
                  fontsize=8.5, color="#1E8449", fontweight="bold", rotation=90)

    # 범례
    all_tenants = p1_tenants + p2_tenants
    patches = [mpatches.Patch(color=C.get(t, "#888"), alpha=0.85,
                               label=("LLM (GPT-2 style)" if t == "LLM" else t.replace("_", " ")))
               for t in all_tenants]
    patches.append(mpatches.Patch(color=C["idle"], alpha=0.6, label="Idle (미사용 SM)"))
    ax_main.legend(handles=patches, loc="upper right", framealpha=0.85,
                   fontsize=8.5, ncol=2)

    # ── fairness 서브플롯 ─────────────────────────────────────────────────────
    # exec_rate을 실제 SM 점유율(%)로 환산: exec_rate% × 50% (파티션 크기)
    for t in p1_tenants + p2_tenants:
        vals = np.array([d.get(f"{t}_exec", 0.0) for d in timeline]) * 0.5
        ax_fair.plot(ts, vals, lw=1.8, color=C.get(t, "#888"),
                     label=t.replace("_", " "))

    ax_fair.set_ylabel("실효 SM 점유율 (%)")
    ax_fair.set_xlabel("경과 시간 (s)")
    ax_fair.set_ylim(0, 55)
    ax_fair.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_fair.legend(loc="upper right", framealpha=0.8, fontsize=8, ncol=len(all_tenants))
    ax_fair.grid(axis="y", alpha=0.2)

    plt.tight_layout(rect=[0, 0, 1, 0.99])
    path = OUT / fname
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] 저장: {path}")


# ─────────────────────────────────────────────────────────────────────────────
#  통합 그래프 (2×2 summary)
# ─────────────────────────────────────────────────────────────────────────────
def plot_summary(ts2, ts3, st2, st3, p1_t2, p2_t2, p1_t3, p2_t3):
    fig = plt.figure(figsize=(18, 12))
    from matplotlib.gridspec import GridSpec
    gs = GridSpec(2, 2, figure=fig, hspace=0.55, wspace=0.35)
    fig.suptitle("KRAKEN 시공간 공유 — 실측 결과\n"
                 "Spatio-Temporal GPU Sharing (Measured)",
                 fontsize=14, fontweight="bold", y=1.01)

    PART = 50  # 파티션 SM 크기 (%)

    def draw_temporal(ax, timeline, tenants, subtitle, weights=None):
        if not timeline: return
        ts_ = np.array([d["ts"] for d in timeline])
        if weights is None:
            weights = {t: 100.0 / len(tenants) for t in tenants}
        for t in tenants:
            vals = np.array([d.get(f"{t}_exec", 0) for d in timeline])
            ax.fill_between(ts_, vals, alpha=0.72, color=C.get(t, "#888"),
                            label=("LLM (GPT-2)" if t == "LLM" else t.replace("_", " ")),
                            linewidth=0)
        for t in tenants:
            ax.axhline(weights[t], ls="--", lw=1.0, color=C.get(t, "#888"), alpha=0.5)

        # squad 경계 추정 (라운드 100ms → 0.1s)
        for sq_t in np.arange(0, ts_[-1], 0.1):
            ax.axvline(sq_t, color="#999", lw=0.5, ls=":", alpha=0.3)

        ax.set_ylim(0, 110)
        ax.set_ylabel("SM 사용률 (%)")
        ax.set_xlabel("경과 시간 (s)")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        ax.legend(loc="upper right", framealpha=0.8, fontsize=8.5)
        ax.grid(axis="y", alpha=0.2)
        ax.set_title(subtitle, fontsize=10, fontweight="bold", pad=8)

    def draw_st(ax, timeline, p1_tenants, p2_tenants, subtitle):
        if not timeline: return
        ts_ = np.array([d["ts"] for d in timeline])
        dt_ = float(np.mean(np.diff(ts_))) if len(ts_) > 1 else 0.5

        P1_LO, P1_HI = 0, 50
        P2_LO, P2_HI = 50, 100

        for i, row in enumerate(timeline):
            x_lo = ts_[i] - dt_ * 0.45
            x_hi = ts_[i] + dt_ * 0.45

            p1_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p1_tenants])
            p1_frac  = p1_execs / (p1_execs.sum() or 1.0)
            used_p1  = min(1.0, p1_execs.sum() / 100.0)
            cur = P1_LO
            for j, t in enumerate(p1_tenants):
                h = p1_frac[j] * used_p1 * (P1_HI - P1_LO)
                if h > 0.05:
                    ax.fill_between([x_lo, x_hi], [cur, cur], [cur+h, cur+h],
                                    color=C.get(t, "#888"), alpha=0.82, linewidth=0)
                cur += h
            if cur < P1_HI:
                ax.fill_between([x_lo, x_hi], [cur, cur], [P1_HI, P1_HI],
                                color=C["idle"], alpha=0.5, linewidth=0)

            p2_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p2_tenants])
            p2_frac  = p2_execs / (p2_execs.sum() or 1.0)
            used_p2  = min(1.0, p2_execs.sum() / 100.0)
            cur = P2_LO
            for j, t in enumerate(p2_tenants):
                h = p2_frac[j] * used_p2 * (P2_HI - P2_LO)
                if h > 0.05:
                    ax.fill_between([x_lo, x_hi], [cur, cur], [cur+h, cur+h],
                                    color=C.get(t, "#888"), alpha=0.82, linewidth=0)
                cur += h
            if cur < P2_HI:
                ax.fill_between([x_lo, x_hi], [cur, cur], [P2_HI, P2_HI],
                                color=C["idle"], alpha=0.5, linewidth=0)

        ax.axhline(50, color="black", lw=2.0, ls="-", zorder=5)
        ax.fill_betweenx([P1_HI, P2_LO], ts_[0]-dt_, ts_[-1]+dt_,
                          color="white", alpha=1.0, zorder=4)
        ax.text((ts_[0]+ts_[-1])/2, 50, "── SM 파티션 경계 ──",
                ha="center", va="center", fontsize=7, color="#555", zorder=6)

        ax.set_ylim(0, 100)
        ax.set_ylabel("SM 공간 (%)")
        ax.set_xlabel("경과 시간 (s)")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        ax.grid(axis="y", alpha=0.2)

        all_t = p1_tenants + p2_tenants
        patches = [mpatches.Patch(color=C.get(t, "#888"), alpha=0.82,
                                   label=("LLM (GPT-2)" if t == "LLM" else t.replace("_"," ")))
                   for t in all_t]
        patches.append(mpatches.Patch(color=C["idle"], alpha=0.5, label="Idle"))
        ax.legend(handles=patches, loc="upper right", framealpha=0.85,
                  fontsize=7.5, ncol=2)

        ax.text(-0.10, 0.25, f"P1\n0~50%", transform=ax.transAxes,
                ha="center", va="center", fontsize=8, color="#C0392B",
                fontweight="bold", rotation=90)
        ax.text(-0.10, 0.75, f"P2\n50~100%", transform=ax.transAxes,
                ha="center", va="center", fontsize=8, color="#1E8449",
                fontweight="bold", rotation=90)
        ax.set_title(subtitle, fontsize=10, fontweight="bold", pad=8)

    # (a)
    ax_a = fig.add_subplot(gs[0, 0])
    draw_temporal(ax_a, ts2, ["LLM", "GEMM"],
                  "(a) 시간 분할  |  2-way: LLM + GEMM\n(OVERCOMMIT, weight 1:1)")

    # (b)
    ax_b = fig.add_subplot(gs[0, 1])
    draw_temporal(ax_b, ts3, ["LLM", "GEMM_A", "GEMM_B"],
                  "(b) 시간 분할  |  3-way: LLM + GEMM_A + GEMM_B\n(OVERCOMMIT, weight 1:1:1)")

    # (c)
    ax_c = fig.add_subplot(gs[1, 0])
    draw_st(ax_c, st2, p1_t2, p2_t2,
            "(c) 시공간 분할  |  2 파티션 × 2 워크로드\n"
            "P1(LLM+GEMM_A) | P2(GEMM_C+GEMM_D),  MPS=50%")

    # (d)
    ax_d = fig.add_subplot(gs[1, 1])
    draw_st(ax_d, st3, p1_t3, p2_t3,
            "(d) 시공간 분할  |  2 파티션 × 3 워크로드\n"
            "P1(LLM+GEMM_A+GEMM_B) | P2(GEMM_C+GEMM_D+GEMM_E),  MPS=50%")

    out_path = OUT / "fig_summary.png"
    plt.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"\n[summary] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters-llm",   type=int, default=80,
                    help="LLM 반복 횟수 (기본 80)")
    ap.add_argument("--iters-gemm",  type=int, default=500,
                    help="GEMM 반복 횟수 (기본 500)")
    ap.add_argument("--sp-iters",    type=int, default=400,
                    help="시공간 분할 GEMM 반복 횟수 (기본 400)")
    ap.add_argument("--size",        type=int, default=2048,
                    help="GEMM 행렬 크기 (기본 2048)")
    ap.add_argument("--interval",    type=float, default=0.5,
                    help="샘플링 간격 초 (기본 0.5)")
    ap.add_argument("--warmup",      type=float, default=3.0,
                    help="워밍업 초 (기본 3.0)")
    ap.add_argument("--only",        choices=["ts2","ts3","st2","st3"], default=None,
                    help="특정 시나리오만 실행")
    args = ap.parse_args()

    print("=" * 60)
    print("KRAKEN 시공간 공유 실측 시각화")
    print("=" * 60)

    ts2_data = ts3_data = st2_data = st3_data = []
    p1_t2 = p2_t2 = p1_t3 = p2_t3 = []

    if args.only in (None, "ts2"):
        ts2_data = collect_ts2(
            iters_llm=args.iters_llm, iters_gemm=args.iters_gemm,
            size=args.size, interval=args.interval, warmup=args.warmup)
        plot_temporal(ts2_data, ["LLM", "GEMM"],
                      "KRAKEN 시간 분할 타임라인 — 2-way (LLM + GEMM)\n(OVERCOMMIT 모드, weight 1:1)",
                      "fig_ts2_timeline.png")

    if args.only in (None, "ts3"):
        ts3_data = collect_ts3(
            iters_llm=args.iters_llm, iters_gemm=args.iters_gemm,
            size=args.size, interval=args.interval, warmup=args.warmup)
        plot_temporal(ts3_data, ["LLM", "GEMM_A", "GEMM_B"],
                      "KRAKEN 시간 분할 타임라인 — 3-way (LLM + GEMM_A + GEMM_B)\n(OVERCOMMIT 모드, weight 1:1:1)",
                      "fig_ts3_timeline.png")

    if args.only in (None, "st2"):
        st2_data, p1_t2, p2_t2 = collect_spatial_temporal(
            n_per_partition=2,
            iters_llm=args.iters_llm, iters_gemm=args.sp_iters,
            size=args.size, interval=args.interval, warmup=args.warmup)
        plot_spatial_temporal(st2_data, p1_t2, p2_t2,
                               "KRAKEN 시공간 분할 — 2 파티션 × 2 워크로드\n"
                               "P1(LLM+GEMM_A, SM 0~50%) | P2(GEMM_C+GEMM_D, SM 50~100%)",
                               "fig_st2_spatial.png")

    if args.only in (None, "st3"):
        st3_data, p1_t3, p2_t3 = collect_spatial_temporal(
            n_per_partition=3,
            iters_llm=args.iters_llm, iters_gemm=args.sp_iters,
            size=args.size, interval=args.interval, warmup=args.warmup)
        plot_spatial_temporal(st3_data, p1_t3, p2_t3,
                               "KRAKEN 시공간 분할 — 2 파티션 × 3 워크로드\n"
                               "P1(LLM+GEMM_A+GEMM_B, SM 0~50%) | P2(GEMM_C~E, SM 50~100%)",
                               "fig_st3_spatial.png")

    if all([ts2_data, ts3_data, st2_data, st3_data]):
        plot_summary(ts2_data, ts3_data, st2_data, st3_data,
                     p1_t2, p2_t2, p1_t3, p2_t3)

    print("\n완료!")
