#!/usr/bin/env python3
"""
KRAKEN 워크로드 공유 시각화
=========================
세 개의 그래프를 results/ 폴더에 저장:

  fig1_timeline.png  : 시간 분할 타임라인 (exec/wait 비율 시계열)
  fig2_ratio.png     : 목표 weight vs 실제 exec 비율 비교
  fig3_spatial.png   : 공간 분할 효과 (MPS% 구성별 실행 시간)

사용:
  python3 visualize_sharing.py
"""
import sys, os, time, subprocess, threading
from pathlib import Path

ROOT   = Path(__file__).parent.parent
TESTS  = Path(__file__).parent
LIB    = ROOT / "runtime" / "gating_lib.so"
WL     = TESTS / "workload_gemm.py"
OUT    = TESTS / "results"
OUT.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT / "shm"))
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "monitor"))

from kraken_shm import (create_shm, MODE_FREE, MODE_OVERCOMMIT,
                        GATE_RUNNING, GATE_KILLER_ACTIVE, GATE_WAITING)
from registry import Registry
from metrics_logger import MetricsLogger

# ── matplotlib 설정 ─────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import numpy as np

# 한글 폰트 설정 (NanumGothic 우선)
_KO_FONT = None
for _fname in ["NanumGothic", "NanumBarunGothic", "NanumSquareRound",
               "Noto Sans CJK KR", "Noto Serif CJK KR"]:
    if any(f.name == _fname for f in fm.fontManager.ttflist):
        _KO_FONT = _fname
        break

plt.rcParams.update({
    "figure.dpi":       150,
    "font.size":        11,
    "axes.titlesize":   13,
    "axes.labelsize":   11,
    "legend.fontsize":  10,
    "axes.spines.top":  False,
    "axes.spines.right": False,
    **({"font.family": _KO_FONT} if _KO_FONT else {}),
})
matplotlib.rcParams["axes.unicode_minus"] = False

COLOR_A  = "#4C72B0"   # 파랑 (테넌트 A)
COLOR_B  = "#DD8452"   # 주황 (테넌트 B)
COLOR_W  = "#CCCCCC"   # 회색 (wait)
GREEN    = "#55A868"
RED_SOFT = "#C44E52"

# ─────────────────────────────────────────────────────────────────────────────
#  유틸
# ─────────────────────────────────────────────────────────────────────────────
def shm_cleanup(path):
    if os.path.exists(path):
        os.unlink(path)

def base_env(extra=None):
    e = os.environ.copy()
    e["LD_PRELOAD"] = str(LIB)
    if extra:
        e.update(extra)
    return e


# ─────────────────────────────────────────────────────────────────────────────
#  데이터 수집: Fig1 + Fig2 (시간 분할, OVERCOMMIT)
# ─────────────────────────────────────────────────────────────────────────────
def collect_timesharing(iters=300, size=2048, sample_interval=0.5, warmup=3.0):
    """
    A(weight 2/3) + B(weight 1/3) OVERCOMMIT 시나리오를 실행하며
    0.5초 간격으로 exec_rate_pct / wait_rate_pct 수집.

    반환:
      timeline : [{"ts": float, "A_exec": float, "A_wait": float,
                             "B_exec": float, "B_wait": float}]
      ratio    : {"A_actual": float, "B_actual": float,
                  "A_target": float, "B_target": float}
    """
    print("\n[collect] OVERCOMMIT 시나리오 시작 (시간 분할 측정)…")

    SHM_NAME = "vis_ts"
    SHM_PATH = f"/dev/shm/kraken_{SHM_NAME}"
    shm_cleanup(SHM_PATH)

    shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
    reg = Registry(shm)
    reg.register("A", virtual_sm=84, virtual_mem_mb=8192, weight=2.0)
    reg.register("B", virtual_sm=84, virtual_mem_mb=8192, weight=1.0)
    shm.policy.weights[0] = 2.0 / 3.0
    shm.policy.weights[1] = 1.0 / 3.0
    shm.policy.version += 1

    print(f"  mode={shm.mode}  (1=OVERCOMMIT)  "
          f"round={shm.policy.round_duration_us}μs")
    print(f"  MPS cap: A={shm.alloc[0].mps_pct}%  B={shm.alloc[1].mps_pct}%")

    e_base = {
        "LD_PRELOAD":    str(LIB),
        "KRAKEN_GROUP":   SHM_NAME,
        "WORKLOAD_ITERS": str(iters),
        "WORKLOAD_SIZE":  str(size),
    }
    env_a = {**os.environ.copy(), **e_base,
             "KRAKEN_TENANT": "A",
             "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[0].mps_pct)}
    env_b = {**os.environ.copy(), **e_base,
             "KRAKEN_TENANT": "B",
             "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[1].mps_pct)}

    pa = subprocess.Popen([sys.executable, str(WL)], env=env_a,
                          stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
    pb = subprocess.Popen([sys.executable, str(WL)], env=env_b,
                          stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

    # MetricsLogger 직접 구성 (open_shm 대신 이미 생성된 shm 재사용)
    logger = MetricsLogger.__new__(MetricsLogger)
    logger.shm = shm; logger._mm = mm
    logger.interval = sample_interval
    logger.stdout = False; logger.output = "/dev/null"
    logger._prev = {}

    timeline = []
    t_start  = time.monotonic()

    print(f"  워밍업 {warmup}s …")
    time.sleep(warmup)
    logger.snapshot()   # prev 초기화

    print("  측정 중…")
    while pa.poll() is None or pb.poll() is None:
        rows = logger.snapshot()
        ts   = time.monotonic() - t_start - warmup
        if rows and ts > 0:
            row_a = next((r for r in rows if r["tenant"] == "A"), None)
            row_b = next((r for r in rows if r["tenant"] == "B"), None)
            if row_a and row_b:
                timeline.append({
                    "ts":     round(ts, 2),
                    "A_exec": row_a["exec_rate_pct"],
                    "A_wait": row_a["wait_rate_pct"],
                    "B_exec": row_b["exec_rate_pct"],
                    "B_wait": row_b["wait_rate_pct"],
                    "A_fair": row_a["fairness"],
                    "B_fair": row_b["fairness"],
                })
                print(f"    t={ts:5.1f}s  "
                      f"A exec={row_a['exec_rate_pct']:5.1f}%  "
                      f"B exec={row_b['exec_rate_pct']:5.1f}%  "
                      f"A fair={row_a['fairness']:.2f}")
        time.sleep(sample_interval)

    pa.wait(timeout=10)
    pb.wait(timeout=10)
    print(f"  완료. 샘플 수={len(timeline)}")

    # 전체 누적 exec으로 비율 계산
    a_exec = shm.tenants[0].total_exec_us
    b_exec = shm.tenants[1].total_exec_us
    total  = a_exec + b_exec or 1
    ratio  = {
        "A_actual": a_exec / total,
        "B_actual": b_exec / total,
        "A_target": 2.0 / 3.0,
        "B_target": 1.0 / 3.0,
    }
    print(f"  실제 비율  A:{ratio['A_actual']:.3f}  B:{ratio['B_actual']:.3f}")
    print(f"  목표 비율  A:{ratio['A_target']:.3f}  B:{ratio['B_target']:.3f}")

    shm_cleanup(SHM_PATH)
    return timeline, ratio


# ─────────────────────────────────────────────────────────────────────────────
#  데이터 수집: Fig3 (공간 분할)
# ─────────────────────────────────────────────────────────────────────────────
def collect_spatial(iters=100, size=2048):
    """
    세 시나리오 실행 시간 측정.
    반환: {"solo_100": float, "solo_50": float, "parallel_50": float}
    """
    print("\n[collect] 공간 분할 시나리오 시작…")

    e_base = os.environ.copy()
    run_args = [sys.executable, str(WL)]

    def run_single(mps_pct):
        env = {**e_base,
               "WORKLOAD_ITERS": str(iters),
               "WORKLOAD_SIZE":  str(size),
               "WORKLOAD_REPORT": "1",
               "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_pct)}
        if os.path.exists(str(LIB)):
            env["LD_PRELOAD"] = str(LIB)
        t0 = time.monotonic()
        subprocess.run(run_args, env=env, capture_output=True, timeout=120)
        return time.monotonic() - t0

    # ① MPS=100% 단독
    print(f"  시나리오 1: MPS=100% 단독 ({iters} iters)…")
    t_100 = run_single(100)
    print(f"    → {t_100:.2f}s")

    # ② MPS=50% 단독
    print(f"  시나리오 2: MPS=50% 단독 ({iters} iters)…")
    t_50 = run_single(50)
    print(f"    → {t_50:.2f}s  ({t_50/t_100:.2f}x)")

    # ③ MPS=50% A+B 동시 (FREE 모드)
    print(f"  시나리오 3: A(50%) + B(50%) 동시 ({iters} iters each)…")
    SHM_NAME = "vis_sp"
    SHM_PATH = f"/dev/shm/kraken_{SHM_NAME}"
    shm_cleanup(SHM_PATH)

    shm, mm = create_shm(SHM_NAME, physical_sm=84, physical_mem_mb=49140)
    reg = Registry(shm)
    reg.register("A", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)
    reg.register("B", virtual_sm=42, virtual_mem_mb=8192, weight=1.0)

    e_par = {**e_base,
             "WORKLOAD_ITERS": str(iters),
             "WORKLOAD_SIZE":  str(size),
             "WORKLOAD_REPORT": "1",
             "KRAKEN_GROUP": SHM_NAME}
    if os.path.exists(str(LIB)):
        e_par["LD_PRELOAD"] = str(LIB)

    env_a = {**e_par, "KRAKEN_TENANT": "A",
             "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[0].mps_pct)}
    env_b = {**e_par, "KRAKEN_TENANT": "B",
             "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(shm.alloc[1].mps_pct)}

    t0 = time.monotonic()
    pa = subprocess.Popen(run_args, env=env_a,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pb = subprocess.Popen(run_args, env=env_b,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pa.wait(timeout=120)
    pb.wait(timeout=120)
    t_par = time.monotonic() - t0
    print(f"    → {t_par:.2f}s  (단독 50% 대비 {t_par/t_50:.2f}x)")

    shm_cleanup(SHM_PATH)

    return {"solo_100": t_100, "solo_50": t_50, "parallel_50": t_par,
            "iters": iters, "size": size}


# ─────────────────────────────────────────────────────────────────────────────
#  Plot 1: 시간 분할 타임라인
# ─────────────────────────────────────────────────────────────────────────────
def plot_timeline(timeline, out_path):
    if not timeline:
        print("[plot1] 데이터 없음, 건너뜀")
        return

    ts    = [d["ts"] for d in timeline]
    a_exec = np.array([d["A_exec"] for d in timeline])
    b_exec = np.array([d["B_exec"] for d in timeline])
    a_wait = np.array([d["A_wait"] for d in timeline])
    b_wait = np.array([d["B_wait"] for d in timeline])
    a_fair = np.array([d["A_fair"] for d in timeline])
    b_fair = np.array([d["B_fair"] for d in timeline])

    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True,
                             gridspec_kw={"height_ratios": [3, 3, 2]})
    fig.suptitle("KRAKEN 시간 분할 타임라인\n(OVERCOMMIT 모드, A:B = 2:1 weight)",
                 fontsize=14, y=0.98)

    # ── 상단: 테넌트 A exec/wait ─────────────────────────────────────────────
    ax = axes[0]
    ax.fill_between(ts, a_exec, alpha=0.7, color=COLOR_A, label="exec")
    ax.fill_between(ts, a_wait, alpha=0.4, color=COLOR_W, label="wait")
    ax.axhline(66.7, ls="--", lw=1.2, color=COLOR_A, alpha=0.6,
               label="목표 exec ~67%")
    ax.set_ylabel("비율 (%)")
    ax.set_ylim(0, 110)
    ax.set_title("테넌트 A  (weight=0.67)", loc="left", fontsize=11)
    ax.legend(loc="upper right", framealpha=0.7)

    # ── 중단: 테넌트 B exec/wait ─────────────────────────────────────────────
    ax = axes[1]
    ax.fill_between(ts, b_exec, alpha=0.7, color=COLOR_B, label="exec")
    ax.fill_between(ts, b_wait, alpha=0.4, color=COLOR_W, label="wait")
    ax.axhline(33.3, ls="--", lw=1.2, color=COLOR_B, alpha=0.6,
               label="목표 exec ~33%")
    ax.set_ylabel("비율 (%)")
    ax.set_ylim(0, 110)
    ax.set_title("테넌트 B  (weight=0.33)", loc="left", fontsize=11)
    ax.legend(loc="upper right", framealpha=0.7)

    # ── 하단: 공정성(fairness) ───────────────────────────────────────────────
    ax = axes[2]
    ax.plot(ts, a_fair, color=COLOR_A, lw=1.8, label="A fairness")
    ax.plot(ts, b_fair, color=COLOR_B, lw=1.8, label="B fairness")
    ax.axhline(1.0, ls="--", lw=1.2, color="black", alpha=0.5,
               label="공정 기준 (1.0)")
    ax.set_ylabel("Fairness")
    ax.set_xlabel("경과 시간 (s)")
    ax.set_ylim(0, 2.2)
    ax.set_title("Fairness  (실제/목표 비율, 1.0 = 완벽)", loc="left", fontsize=11)
    ax.legend(loc="upper right", framealpha=0.7)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot1] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  Plot 2: 목표 weight vs 실제 exec 비율
# ─────────────────────────────────────────────────────────────────────────────
def plot_ratio(ratio, out_path):
    tenants = ["A", "B"]
    targets = [ratio["A_target"] * 100, ratio["B_target"] * 100]
    actuals = [ratio["A_actual"] * 100, ratio["B_actual"] * 100]

    x   = np.arange(len(tenants))
    w   = 0.32

    fig, ax = plt.subplots(figsize=(7, 5))
    bars_t = ax.bar(x - w/2, targets, w, label="목표 비율 (weight)",
                    color=[COLOR_A, COLOR_B], alpha=0.45, edgecolor="white")
    bars_a = ax.bar(x + w/2, actuals, w, label="실제 exec 비율",
                    color=[COLOR_A, COLOR_B], alpha=0.9,  edgecolor="white")

    for bar, val in zip(bars_t, targets):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.0,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=10,
                color="gray")
    for bar, val in zip(bars_a, actuals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.0,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=10,
                fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels([f"테넌트 {t}" for t in tenants])
    ax.set_ylabel("GPU 실행 비율 (%)")
    ax.set_ylim(0, 100)
    ax.set_title("KRAKEN 시간 분할 — 목표 vs 실제 GPU 점유 비율\n"
                 "(OVERCOMMIT 모드, A:B = 2:1 weight)")
    ax.legend()

    # 오차 표시
    for i, (t, a) in enumerate(zip(targets, actuals)):
        err = abs(a - t)
        color = GREEN if err < 10 else RED_SOFT
        ax.annotate(f"Δ={err:.1f}%p", xy=(i + w/2, a + 2),
                    xytext=(i + w/2 + 0.05, a + 9),
                    fontsize=9, color=color,
                    arrowprops=dict(arrowstyle="->", color=color, lw=1.0))

    plt.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot2] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  Plot 3: 공간 분할 효과
# ─────────────────────────────────────────────────────────────────────────────
def plot_spatial(data, out_path):
    labels = [
        f"단독\nMPS=100%\n(기준)",
        f"단독\nMPS=50%\n(SM 절반)",
        f"A(50%) + B(50%)\n동시 실행\n(공간 분할)",
    ]
    times  = [data["solo_100"], data["solo_50"], data["parallel_50"]]
    colors = [GREEN, COLOR_A, COLOR_B]

    norm = data["solo_100"]   # 기준 = 단독 100%

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5))
    fig.suptitle(f"KRAKEN 공간 분할 효과 — MPS 구성별 성능 비교\n"
                 f"(GEMM {data['size']}×{data['size']},  {data['iters']} iters)")

    # 왼쪽: 절대 실행 시간
    bars = ax1.bar(range(3), times, color=colors, alpha=0.85, width=0.55,
                   edgecolor="white")
    for bar, t in zip(bars, times):
        ax1.text(bar.get_x() + bar.get_width()/2, t + 0.05,
                 f"{t:.2f}s", ha="center", va="bottom", fontsize=11,
                 fontweight="bold")
    ax1.set_xticks(range(3))
    ax1.set_xticklabels(labels, fontsize=9)
    ax1.set_ylabel("실행 시간 (s)")
    ax1.set_title("절대 실행 시간")

    # 오른쪽: 기준 대비 상대 시간 + 처리량 효율
    rel_times  = [t / norm for t in times]
    # 처리량 효율: 동일 시간 안에 처리한 총 작업량 / 기준
    #   - 단독 100%: 효율=1.0
    #   - 단독 50% : 효율=1/rel_times[1] * 1 (한 테넌트만)
    #   - 병렬 50% : 효율=1/rel_times[2] * 2 (두 테넌트 동시) / 기준
    throughput_eff = [
        1.0,
        1.0 / rel_times[1],
        2.0 / rel_times[2],
    ]

    x  = np.arange(3)
    w  = 0.38
    b1 = ax2.bar(x - w/2, rel_times, w, label="상대 실행 시간 (낮을수록 좋음)",
                 color=colors, alpha=0.5, edgecolor="white")
    b2 = ax2.bar(x + w/2, throughput_eff, w,
                 label="총 처리량 효율 (높을수록 좋음)",
                 color=colors, alpha=0.9, edgecolor="white")

    for bar, v in zip(b1, rel_times):
        ax2.text(bar.get_x() + bar.get_width()/2, v + 0.02,
                 f"{v:.2f}x", ha="center", va="bottom", fontsize=9)
    for bar, v in zip(b2, throughput_eff):
        ax2.text(bar.get_x() + bar.get_width()/2, v + 0.02,
                 f"{v:.2f}x", ha="center", va="bottom", fontsize=9,
                 fontweight="bold")

    ax2.axhline(1.0, ls="--", lw=1.2, color="black", alpha=0.4)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=9)
    ax2.set_ylabel("배율 (기준=1.0)")
    ax2.set_title("기준 대비 상대 시간 & 처리량 효율")
    ax2.legend(fontsize=8)

    # 공간 분할 효과 강조 박스
    effect = throughput_eff[2]
    color  = GREEN if effect >= 1.5 else "#F4A261"
    ax2.annotate(f"공간 분할 효과\n처리량 {effect:.2f}x",
                 xy=(2 + w/2, throughput_eff[2]),
                 xytext=(1.6, throughput_eff[2] + 0.3),
                 fontsize=9, color=color, fontweight="bold",
                 arrowprops=dict(arrowstyle="->", color=color, lw=1.2))

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot3] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not os.path.exists(str(LIB)):
        print(f"[경고] gating_lib.so 없음: {LIB}")
        print("       공간 분할 시나리오는 MPS 제한만으로 실행됩니다.")

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters",    type=int,   default=800,
                    help="시간 분할 반복 횟수 (기본 800)")
    ap.add_argument("--size",     type=int,   default=2048,
                    help="행렬 크기 (기본 2048)")
    ap.add_argument("--sp-iters", type=int,   default=100,
                    help="공간 분할 반복 횟수 (기본 100)")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="샘플링 간격 (초, 기본 0.5)")
    ap.add_argument("--warmup",   type=float, default=3.0,
                    help="워밍업 시간 (초, 기본 3.0)")
    ap.add_argument("--only",     choices=["ts", "sp"], default=None,
                    help="ts=시간분할만, sp=공간분할만")
    args = ap.parse_args()

    skip_ts = args.only == "sp"
    skip_sp = args.only == "ts"

    timeline, ratio, spatial_data = [], {}, {}

    if not skip_ts:
        timeline, ratio = collect_timesharing(
            iters=args.iters, size=args.size,
            sample_interval=args.interval, warmup=args.warmup)

    if not skip_sp:
        spatial_data = collect_spatial(iters=args.sp_iters, size=args.size)

    print("\n[plot] 그래프 생성 중…")

    if timeline:
        plot_timeline(timeline, OUT / "fig1_timeline.png")

    if ratio:
        plot_ratio(ratio, OUT / "fig2_ratio.png")

    if spatial_data:
        plot_spatial(spatial_data, OUT / "fig3_spatial.png")

    saved = list(OUT.glob("fig*.png"))
    print(f"\n완료! 저장된 파일 ({len(saved)}개):")
    for f in sorted(saved):
        print(f"  {f}")
