#!/usr/bin/env python3
"""
KRAKEN 시공간 공유 시각화 — 실제 모델 (SM 50/50 분할)
=====================================================
시나리오:
  Partition 1 (SM 0~50%):  LLM (GPT-2 style) + ResNet-50
  Partition 2 (SM 50~100%): ResNet-50 + ViT-B/16 + EfficientNet-B0

각 파티션 내부에서 OVERCOMMIT 시간 분할 스케줄링.
MPS 50% cap으로 물리 SM 절반씩 사용.

출력:
  results/fig_summary.png  — SM 공간 + 오퍼레이터 타임라인 통합 그래프

사용:
  cd kraken/tests
  python3 visualize_spatial_models.py [--interval S] [--warmup S]
"""
import sys, os, time, subprocess, threading, tempfile, shutil
from pathlib import Path

ROOT  = Path(__file__).parent.parent
TESTS = Path(__file__).parent
LIB   = ROOT / "runtime" / "gating_lib.so"
OUT   = TESTS / "results"
OUT.mkdir(exist_ok=True)

WL = {
    "LLM":         TESTS / "workload_llm.py",
    "ResNet50":    TESTS / "workload_resnet.py",
    "ViT":         TESTS / "workload_vit.py",
    "EfficientNet":TESTS / "workload_efficientnet.py",
}

sys.path.insert(0, str(ROOT / "shm"))
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "monitor"))

from kraken_shm import create_shm, MODE_OVERCOMMIT, GATE_RUNNING
from registry   import Registry
from metrics_logger import MetricsLogger

# ── matplotlib / 한글 폰트 ────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import numpy as np

_font_path = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"
if os.path.exists(_font_path):
    fm.fontManager.addfont(_font_path)
    _KO = fm.FontProperties(fname=_font_path).get_name()
    plt.rcParams["font.family"] = _KO
matplotlib.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({
    "figure.dpi":       150,
    "font.size":        10,
    "axes.titlesize":   11,
    "axes.labelsize":   10,
    "legend.fontsize":  9,
    "axes.spines.top":  False,
    "axes.spines.right": False,
})

PHYSICAL_SM = 84   # A6000

# ── 모델/오퍼레이터 색상 팔레트 ──────────────────────────────────────────────
# 모델별 기본색
MODEL_COLOR = {
    "LLM":          "#E74C3C",   # 빨강
    "ResNet50":     "#3498DB",   # 파랑 (P1)
    "ResNet50_P2":  "#2ECC71",   # 초록 (P2, 구분용)
    "ViT":          "#9B59B6",   # 보라
    "EfficientNet": "#F39C12",   # 주황
    "idle":         "#EEEEEE",
}

# 오퍼레이터 타입별 색상 (Gantt 차트용)
OP_COLOR = {
    # LLM
    "Prefill.QKV":   "#C0392B",
    "Prefill.Attn":  "#E74C3C",
    "Prefill.FFN":   "#EC7063",
    "Decode.QKV":    "#922B21",
    "Decode.Proj":   "#A93226",
    # ResNet
    "Conv1_BN":      "#1A5276",
    "Layer1_Res":    "#2471A3",
    "Layer2_Res":    "#2E86C1",
    "Layer3_Res":    "#3498DB",
    "Layer4_Res":    "#5DADE2",
    "AvgPool":       "#85C1E9",
    "FC":            "#1ABC9C",
    # ViT
    "PatchEmbed":    "#6C3483",
    "Head":          "#7D3C98",
}
# ViT Attn / MLP 블록 (12개)
for _i in range(12):
    OP_COLOR[f"Attn{_i}"] = "#8E44AD"
    OP_COLOR[f"MLP{_i}"]  = "#A569BD"
# EfficientNet 블록
_EFFNET_COLORS = {
    "StemConv":   "#D35400",
    "MBConv1":    "#E67E22",
    "MBConv6_2":  "#EB984E",
    "MBConv6_3":  "#F0A87A",
    "MBConv6_4":  "#F5CBA7",
    "MBConv6_5":  "#FDEBD0",
    "MBConv6_6":  "#FAD7A0",
    "MBConv6_7":  "#F8C471",
    "HeadConv":   "#F39C12",
    "Classifier": "#D68910",
}
OP_COLOR.update(_EFFNET_COLORS)

# 오퍼레이터 타입 → 그룹 (범례용)
OP_GROUP = {}
for k in ["Prefill.QKV","Prefill.Attn","Prefill.FFN"]:
    OP_GROUP[k] = "LLM Prefill"
for k in ["Decode.QKV","Decode.Proj"]:
    OP_GROUP[k] = "LLM Decode"
for k in ["Conv1_BN","Layer1_Res","Layer2_Res","Layer3_Res","Layer4_Res","AvgPool"]:
    OP_GROUP[k] = "ResNet Conv/Pool"
OP_GROUP["FC"] = "Linear/Head"
OP_GROUP["PatchEmbed"] = "ViT PatchEmbed"
OP_GROUP["Head"] = "Linear/Head"
for _i in range(12):
    OP_GROUP[f"Attn{_i}"] = "ViT Attention"
    OP_GROUP[f"MLP{_i}"]  = "ViT MLP"
for k in _EFFNET_COLORS:
    OP_GROUP[k] = "EfficientNet MBConv"
OP_GROUP["Classifier"] = "Linear/Head"


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

def preinit(shm, n):
    for i in range(n):
        w   = shm.policy.weights[i]
        tot = sum(shm.policy.weights[j] for j in range(n)) or 1.0
        shm.tenants[i].time_remain_us = int(shm.policy.round_duration_us * w / tot)
        shm.tenants[i].gate_state = GATE_RUNNING

def make_logger(shm, mm, interval):
    lg = MetricsLogger.__new__(MetricsLogger)
    lg.shm = shm; lg._mm = mm
    lg.interval = interval
    lg.stdout = False; lg.output = "/dev/null"
    lg._prev = {}
    return lg

def wait_ready(ready_files, timeout=90.0):
    t0 = time.monotonic()
    while True:
        if all(os.path.exists(f) for f in ready_files):
            return True
        if time.monotonic() - t0 > timeout:
            missing = [f for f in ready_files if not os.path.exists(f)]
            print(f"  [배리어] 타임아웃 — 미준비: {missing}")
            return False
        time.sleep(0.1)


# ─────────────────────────────────────────────────────────────────────────────
#  실험 실행: P1(LLM+ResNet50) | P2(ResNet50+ViT+EfficientNet)
# ─────────────────────────────────────────────────────────────────────────────
def run_experiment(interval=0.3, warmup=4.0,
                   iters_llm=120, iters_resnet_p1=80, iters_resnet_p2=80,
                   iters_vit=60, iters_effnet=100):
    """
    두 파티션을 동시에 운용하며 metrics + operator trace 수집.
    반환: (timeline, traces, t_start_abs)
      timeline: list[dict]  — 시간대별 exec_rate
      traces:   dict[tenant → list[(rel_ts, phase)]]
      t_start_abs: 실험 시작 절대 시각 (time.time())
    """
    MPS     = 50
    PART_SM = PHYSICAL_SM // 2   # 42

    SHM1 = "vsm_p1"; SHM2 = "vsm_p2"
    P1   = f"/dev/shm/kraken_{SHM1}"
    P2   = f"/dev/shm/kraken_{SHM2}"
    shm_cleanup(P1, P2)

    # ── SHM 생성 ─────────────────────────────────────────────────────────────
    shm1, mm1 = create_shm(SHM1, physical_sm=PART_SM, physical_mem_mb=20000)
    reg1 = Registry(shm1)
    reg1.register("LLM",      virtual_sm=PART_SM, virtual_mem_mb=6000, weight=1.0)
    reg1.register("ResNet50", virtual_sm=PART_SM, virtual_mem_mb=6000, weight=1.0)
    shm1.policy.weights[0] = 0.5
    shm1.policy.weights[1] = 0.5
    shm1.policy.version += 1
    preinit(shm1, 2)

    shm2, mm2 = create_shm(SHM2, physical_sm=PART_SM, physical_mem_mb=20000)
    reg2 = Registry(shm2)
    reg2.register("ResNet50_P2",  virtual_sm=PART_SM, virtual_mem_mb=5000, weight=1.0)
    reg2.register("ViT",          virtual_sm=PART_SM, virtual_mem_mb=5000, weight=1.0)
    reg2.register("EfficientNet", virtual_sm=PART_SM, virtual_mem_mb=5000, weight=1.0)
    for i in range(3):
        shm2.policy.weights[i] = 1.0 / 3.0
    shm2.policy.version += 1
    preinit(shm2, 3)

    print(f"\n[실험] P1 mode={shm1.mode}  P2 mode={shm2.mode}  MPS={MPS}%/파티션")
    print(f"  P1: LLM(w=0.5) + ResNet50(w=0.5)")
    print(f"  P2: ResNet50_P2(w=1/3) + ViT(w=1/3) + EfficientNet(w=1/3)")

    tmpdir = tempfile.mkdtemp(prefix="kraken_spatial_")

    def rfile(name):
        return os.path.join(tmpdir, f"ready_{name}")
    def tfile(name):
        return os.path.join(tmpdir, f"trace_{name}.csv")

    barrier = os.path.join(tmpdir, "barrier_start")

    all_cfg = [
        # (tenant_name, group, model_key, iters, ready_file, trace_file)
        ("LLM",         SHM1, "LLM",         iters_llm,       rfile("LLM"),    tfile("LLM")),
        ("ResNet50",    SHM1, "ResNet50",     iters_resnet_p1, rfile("RN_P1"),  tfile("ResNet50")),
        ("ResNet50_P2", SHM2, "ResNet50",     iters_resnet_p2, rfile("RN_P2"),  tfile("ResNet50_P2")),
        ("ViT",         SHM2, "ViT",          iters_vit,       rfile("ViT"),    tfile("ViT")),
        ("EfficientNet",SHM2, "EfficientNet", iters_effnet,    rfile("EffNet"), tfile("EfficientNet")),
    ]

    procs = []
    for (tenant, grp, model_key, iters, rf, tf) in all_cfg:
        env = make_env({
            "KRAKEN_TENANT":            tenant,
            "KRAKEN_GROUP":             grp,
            "WORKLOAD_ITERS":          str(iters),
            "WORKLOAD_BATCH":          "16" if model_key in ("ResNet50","EfficientNet") else "8",
            "WORKLOAD_REPORT":         "1",
            "WORKLOAD_READY":          rf,
            "WORKLOAD_BARRIER":        barrier,
            "WORKLOAD_TRACE_FILE":     tf,
            "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(MPS),
        })
        wl_path = WL[model_key]
        p = subprocess.Popen(
            [sys.executable, str(wl_path)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(p)

    # ── 모델 로드 대기 ────────────────────────────────────────────────────────
    ready_files = [cfg[4] for cfg in all_cfg]
    print(f"  모델 로드 대기 중 (최대 90s)…")
    ok = wait_ready(ready_files, timeout=90.0)
    if not ok:
        print("  일부 모델 로드 실패, 계속 진행…")

    # ── 배리어 열기 ───────────────────────────────────────────────────────────
    open(barrier, "w").close()
    t_start_abs = time.time()
    print(f"  배리어 열림 → 동시 시작!")

    # ── MetricsLogger 폴링 (두 SHM 동시) ─────────────────────────────────────
    lg1 = make_logger(shm1, mm1, interval)
    lg2 = make_logger(shm2, mm2, interval)

    timeline = []
    t0       = time.monotonic()
    lock     = threading.Lock()
    _buf1    = {}
    _buf2    = {}

    def poll1():
        time.sleep(warmup)
        lg1.snapshot()
        while any(p.poll() is None for p in procs):
            rows = lg1.snapshot()
            with lock:
                for r in (rows or []):
                    _buf1[r["tenant"]] = r
            time.sleep(interval)

    def poll2():
        time.sleep(warmup)
        lg2.snapshot()
        while any(p.poll() is None for p in procs):
            rows = lg2.snapshot()
            with lock:
                for r in (rows or []):
                    _buf2[r["tenant"]] = r
            time.sleep(interval)

    th1 = threading.Thread(target=poll1, daemon=True)
    th2 = threading.Thread(target=poll2, daemon=True)
    th1.start(); th2.start()

    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup + interval)
    print("  측정 중…")

    p1_names = ["LLM", "ResNet50"]
    p2_names = ["ResNet50_P2", "ViT", "EfficientNet"]
    all_names = p1_names + p2_names

    while any(p.poll() is None for p in procs):
        ts = round(time.monotonic() - t0 - warmup, 2)
        with lock:
            b1 = dict(_buf1); b2 = dict(_buf2)
        if ts > 0 and b1 and b2:
            row = {"ts": ts}
            for n in all_names:
                buf = b1 if n in p1_names else b2
                row[f"{n}_exec"] = buf.get(n, {}).get("exec_rate_pct", 0.0)
                row[f"{n}_wait"] = buf.get(n, {}).get("wait_rate_pct", 0.0)
            timeline.append(row)
            p1_str = " ".join(f"{n}={row[f'{n}_exec']:5.1f}%" for n in p1_names)
            p2_str = " ".join(f"{n}={row[f'{n}_exec']:5.1f}%" for n in p2_names)
            print(f"    t={ts:5.1f}s  P1[{p1_str}]  P2[{p2_str}]")
        time.sleep(interval)

    for p in procs:
        p.wait(timeout=30)

    print(f"  완료. 샘플={len(timeline)}")

    # ── stdout 출력 수집 ──────────────────────────────────────────────────────
    for i, (tenant, *_rest) in enumerate(all_cfg):
        out = procs[i].stdout.read().decode().strip()
        err = procs[i].stderr.read().decode().strip()
        if out:
            print(f"  [{tenant}] {out}")
        if err:
            print(f"  [{tenant}] stderr: {err[:200]}")

    # ── trace 파일 읽기 ───────────────────────────────────────────────────────
    traces = {}
    for (tenant, grp, model_key, iters, rf, tf) in all_cfg:
        rows = []
        if os.path.exists(tf):
            with open(tf) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(",", 1)
                    if len(parts) == 2:
                        try:
                            rows.append((float(parts[0]), parts[1]))
                        except ValueError:
                            pass
        traces[tenant] = rows
        print(f"  trace [{tenant}]: {len(rows)} 이벤트")

    shm_cleanup(P1, P2)
    shutil.rmtree(tmpdir, ignore_errors=True)

    return timeline, traces, t_start_abs, all_cfg


# ─────────────────────────────────────────────────────────────────────────────
#  Trace 데이터를 시간 구간별로 빈닝
# ─────────────────────────────────────────────────────────────────────────────
def bin_trace(trace_rows, t_start_abs, t_end_abs, bin_size=0.2):
    """
    trace_rows: [(abs_wall_time, phase), ...]
    t_start_abs: 실험 시작 절대 시각 (time.time())
    반환: list[(rel_ts_lo, rel_ts_hi, dominant_phase)]
    """
    if not trace_rows:
        return []

    # 상대 시간으로 변환
    rel_rows = [(ts - t_start_abs, phase) for ts, phase in trace_rows
                if t_start_abs <= ts <= t_end_abs]
    if not rel_rows:
        return []

    t_max = rel_rows[-1][0]
    bins  = []
    t     = 0.0
    while t < t_max:
        t_lo = t
        t_hi = t + bin_size
        # 이 구간에 속하는 이벤트 수집
        in_bin = [(ts, ph) for ts, ph in rel_rows if t_lo <= ts < t_hi]
        if in_bin:
            # 가장 많이 나온 phase를 dominant로
            from collections import Counter
            counts = Counter(ph for _, ph in in_bin)
            dominant = counts.most_common(1)[0][0]
            bins.append((t_lo, t_hi, dominant))
        t += bin_size
    return bins


# ─────────────────────────────────────────────────────────────────────────────
#  통합 그래프 생성
# ─────────────────────────────────────────────────────────────────────────────
def plot_all(timeline, traces, t_start_abs, all_cfg):
    if not timeline:
        print("[plot] 타임라인 데이터 없음, 그래프 생략")
        return

    ts_arr   = np.array([d["ts"] for d in timeline])
    t_total  = float(ts_arr[-1]) if len(ts_arr) > 0 else 30.0
    t_end_abs = t_start_abs + t_total + 4.0  # warmup 포함

    all_tenants = ["LLM", "ResNet50", "ResNet50_P2", "ViT", "EfficientNet"]
    p1_tenants  = ["LLM", "ResNet50"]
    p2_tenants  = ["ResNet50_P2", "ViT", "EfficientNet"]

    # ── 레이아웃 ─────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(18, 16))
    from matplotlib.gridspec import GridSpec
    gs = GridSpec(3, 1, figure=fig, height_ratios=[4, 3, 1.8],
                  hspace=0.50)

    ax_sm   = fig.add_subplot(gs[0])   # SM 공간 분할
    ax_op   = fig.add_subplot(gs[1])   # 오퍼레이터 타임라인
    ax_util = fig.add_subplot(gs[2])   # SM 점유율 선 그래프

    fig.suptitle(
        "KRAKEN 시공간 공유 — 실제 모델 실측\n"
        "SM 50/50 분할:  P1(LLM + ResNet-50)  |  P2(ResNet-50 + ViT-B/16 + EfficientNet-B0)",
        fontsize=13, fontweight="bold", y=1.01)

    dt = float(np.mean(np.diff(ts_arr))) if len(ts_arr) > 1 else 0.3

    # ═══════════════════════════════════════════════════════════════
    #  Panel 1: SM 공간×시간 다이어그램
    # ═══════════════════════════════════════════════════════════════
    P1_LO, P1_HI = 0,  50
    P2_LO, P2_HI = 50, 100

    legend_patches_sm = []

    for i, row in enumerate(timeline):
        x_lo = ts_arr[i] - dt * 0.45
        x_hi = ts_arr[i] + dt * 0.45

        # ── Partition 1 (0~50%) ───────────────────────────────────
        p1_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p1_tenants])
        p1_total = p1_execs.sum() or 1.0
        p1_fracs = p1_execs / p1_total
        used_p1  = min(1.0, p1_execs.sum() / 100.0)

        cursor = P1_LO
        for j, t in enumerate(p1_tenants):
            h = p1_fracs[j] * used_p1 * (P1_HI - P1_LO)
            if h > 0.05:
                ax_sm.fill_between([x_lo, x_hi], [cursor, cursor],
                                   [cursor+h, cursor+h],
                                   color=MODEL_COLOR.get(t, "#888"),
                                   alpha=0.85, linewidth=0)
            cursor += h
        if cursor < P1_HI:
            ax_sm.fill_between([x_lo, x_hi], [cursor, cursor],
                               [P1_HI, P1_HI],
                               color=MODEL_COLOR["idle"], alpha=0.6, linewidth=0)

        # ── Partition 2 (50~100%) ─────────────────────────────────
        p2_execs = np.array([row.get(f"{t}_exec", 0.0) for t in p2_tenants])
        p2_total = p2_execs.sum() or 1.0
        p2_fracs = p2_execs / p2_total
        used_p2  = min(1.0, p2_execs.sum() / 100.0)

        cursor = P2_LO
        for j, t in enumerate(p2_tenants):
            h = p2_fracs[j] * used_p2 * (P2_HI - P2_LO)
            if h > 0.05:
                ax_sm.fill_between([x_lo, x_hi], [cursor, cursor],
                                   [cursor+h, cursor+h],
                                   color=MODEL_COLOR.get(t, "#888"),
                                   alpha=0.85, linewidth=0)
            cursor += h
        if cursor < P2_HI:
            ax_sm.fill_between([x_lo, x_hi], [cursor, cursor],
                               [P2_HI, P2_HI],
                               color=MODEL_COLOR["idle"], alpha=0.6, linewidth=0)

    # SM 파티션 경계선
    ax_sm.axhline(50, color="black", lw=2.5, ls="-", zorder=5)
    if len(ts_arr) > 1:
        ax_sm.fill_betweenx([P1_HI - 0.5, P2_LO + 0.5],
                             ts_arr[0] - dt, ts_arr[-1] + dt,
                             color="white", alpha=1.0, zorder=4)
        ax_sm.text((ts_arr[0] + ts_arr[-1]) / 2, 50,
                   "── SM 파티션 경계 (MPS = 50%) ──",
                   ha="center", va="center", fontsize=9,
                   color="#333", zorder=6, fontweight="bold")

    ax_sm.set_ylim(0, 100)
    ax_sm.set_ylabel("SM 공간 사용률 (%)", fontsize=10)
    ax_sm.set_yticks([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    ax_sm.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_sm.grid(axis="y", alpha=0.2)
    ax_sm.set_title("(1) SM 공간 분할 × 시간 다이어그램 — 모델별 SM 점유",
                    fontsize=11, fontweight="bold", loc="left", pad=8)

    # 파티션 라벨
    ax_sm.text(-0.075, 0.25,
               "Partition 1\n(SM 0~50%)\nLLM + ResNet",
               transform=ax_sm.transAxes, ha="center", va="center",
               fontsize=8.5, color="#C0392B", fontweight="bold", rotation=90)
    ax_sm.text(-0.075, 0.75,
               "Partition 2\n(SM 50~100%)\nResNet+ViT+EffNet",
               transform=ax_sm.transAxes, ha="center", va="center",
               fontsize=8.5, color="#1E8449", fontweight="bold", rotation=90)

    # 범례
    display_names = {
        "LLM":          "LLM (GPT-2 style)",
        "ResNet50":     "ResNet-50  [P1]",
        "ResNet50_P2":  "ResNet-50  [P2]",
        "ViT":          "ViT-B/16",
        "EfficientNet": "EfficientNet-B0",
    }
    patches_sm = [
        mpatches.Patch(color=MODEL_COLOR[t], alpha=0.85, label=display_names[t])
        for t in all_tenants
    ]
    patches_sm.append(
        mpatches.Patch(color=MODEL_COLOR["idle"], alpha=0.6, label="Idle (미사용 SM)"))
    ax_sm.legend(handles=patches_sm, loc="upper right",
                 framealpha=0.88, fontsize=8.5, ncol=3)

    # ═══════════════════════════════════════════════════════════════
    #  Panel 2: 오퍼레이터 타임라인 (Gantt)
    # ═══════════════════════════════════════════════════════════════
    BIN = 0.25   # 초 단위 빈 크기

    # Y축: 각 모델이 하나의 행 (아래→위 순서)
    GANTT_ROWS = {
        "LLM":          4,
        "ResNet50":     3,
        "ResNet50_P2":  1,   # P2 구분을 위해 간격
        "ViT":          0,
        "EfficientNet": -1,
    }
    ROW_HEIGHT = 0.75
    ROW_LABELS = {
        "LLM":          "LLM\n(Prefill/Decode)",
        "ResNet50":     "ResNet-50\n[P1]",
        "ResNet50_P2":  "ResNet-50\n[P2]",
        "ViT":          "ViT-B/16",
        "EfficientNet": "EfficientNet-B0",
    }

    # exec_rate를 lookup으로 만들기 (Gantt에서 exec 상태만 표시)
    def exec_at(tenant, rel_ts):
        """해당 시간대의 exec_rate (%) 반환"""
        if not timeline:
            return 0.0
        idx = np.searchsorted(ts_arr, rel_ts)
        idx = min(idx, len(timeline) - 1)
        return timeline[idx].get(f"{tenant}_exec", 0.0)

    # 각 모델 trace 빈닝 후 Gantt 바 그리기
    collected_op_groups = set()

    for tenant in all_tenants:
        row_y    = GANTT_ROWS[tenant]
        trace    = traces.get(tenant, [])
        bins     = bin_trace(trace, t_start_abs, t_end_abs, bin_size=BIN)
        bg_color = MODEL_COLOR.get(tenant, "#CCCCCC")

        # 배경: exec 구간을 옅은 모델색으로 표시
        for row in timeline:
            ts_v = row["ts"]
            ex   = row.get(f"{tenant}_exec", 0.0)
            if ex > 5.0:   # 5% 이상 exec일 때
                ax_op.barh(row_y, BIN, left=ts_v - BIN/2,
                           height=ROW_HEIGHT * 0.98,
                           color=bg_color, alpha=0.18, linewidth=0)

        # 전경: operator phase 색상
        for (t_lo, t_hi, phase) in bins:
            # exec 상태인지 확인
            ex = exec_at(tenant, (t_lo + t_hi) / 2)
            if ex < 3.0:
                continue
            op_col = OP_COLOR.get(phase, "#AAAAAA")
            ax_op.barh(row_y, t_hi - t_lo, left=t_lo,
                       height=ROW_HEIGHT * 0.80,
                       color=op_col, alpha=0.90, linewidth=0.3,
                       edgecolor="white")
            grp = OP_GROUP.get(phase, phase)
            collected_op_groups.add((grp, op_col))

    # P1/P2 구분선
    p1_p2_boundary = (GANTT_ROWS["ResNet50"] + GANTT_ROWS["ResNet50_P2"]) / 2
    ax_op.axhline(p1_p2_boundary, color="#555", lw=1.5, ls="--", alpha=0.7)
    ax_op.text(t_total * 0.98, p1_p2_boundary + 0.05,
               "── P1 / P2 경계 ──",
               ha="right", va="bottom", fontsize=7.5, color="#555", style="italic")

    # Y축 레이블 (각 행 이름)
    ytick_pos    = [GANTT_ROWS[t] for t in all_tenants]
    ytick_labels = [ROW_LABELS[t] for t in all_tenants]
    ax_op.set_yticks(ytick_pos)
    ax_op.set_yticklabels(ytick_labels, fontsize=8.5)
    ax_op.set_ylim(GANTT_ROWS["EfficientNet"] - 0.8,
                   GANTT_ROWS["LLM"] + 0.8)
    ax_op.set_ylabel("모델 (테넌트)", fontsize=10)
    ax_op.set_xlabel("경과 시간 (s)", fontsize=10)
    ax_op.grid(axis="x", alpha=0.2)
    ax_op.set_title(
        "(2) 오퍼레이터 타임라인 — 시간대별 실행 오퍼레이터\n"
        "(색상: 오퍼레이터 타입,  투명도: exec 상태일 때만 표시)",
        fontsize=11, fontweight="bold", loc="left", pad=8)
    ax_op.set_xlim(0, t_total)

    # 오퍼레이터 범례 (그룹별)
    op_legend_items = sorted(collected_op_groups, key=lambda x: x[0])
    op_patches = [mpatches.Patch(color=c, alpha=0.9, label=g)
                  for g, c in op_legend_items]
    if op_patches:
        ax_op.legend(handles=op_patches, loc="upper right",
                     framealpha=0.88, fontsize=7.5, ncol=2,
                     title="오퍼레이터 그룹", title_fontsize=8)

    # ═══════════════════════════════════════════════════════════════
    #  Panel 3: 실효 SM 점유율 선 그래프
    # ═══════════════════════════════════════════════════════════════
    # exec_rate_pct × 0.5 (파티션 크기) = 실효 GPU SM 점유율
    for t in all_tenants:
        vals = np.array([row.get(f"{t}_exec", 0.0) for row in timeline]) * 0.5
        ax_util.plot(ts_arr, vals,
                     lw=1.8, color=MODEL_COLOR.get(t, "#888"),
                     label=display_names[t], alpha=0.9)

    ax_util.set_ylim(0, 55)
    ax_util.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_util.set_ylabel("실효 GPU SM\n점유율 (%)", fontsize=9)
    ax_util.set_xlabel("경과 시간 (s)", fontsize=10)
    ax_util.legend(loc="upper right", framealpha=0.88,
                   fontsize=7.5, ncol=3)
    ax_util.grid(axis="y", alpha=0.2)
    ax_util.set_title("(3) 모델별 실효 SM 점유율  (= exec_rate × 50%)",
                      fontsize=11, fontweight="bold", loc="left", pad=6)
    ax_util.set_xlim(0, t_total)

    # ── 저장 ─────────────────────────────────────────────────────────────────
    out_path = OUT / "fig_summary.png"
    plt.tight_layout(rect=[0, 0, 1, 0.99])
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"\n[결과] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="KRAKEN 시공간 공유 시각화 — 실제 모델")
    ap.add_argument("--interval",       type=float, default=0.3,
                    help="샘플링 간격 (s, 기본 0.3)")
    ap.add_argument("--warmup",         type=float, default=4.0,
                    help="워밍업 (s, 기본 4.0)")
    ap.add_argument("--iters-llm",      type=int,   default=120,
                    help="LLM 반복 횟수 (기본 120)")
    ap.add_argument("--iters-resnet-p1",type=int,   default=80,
                    help="ResNet-50 P1 반복 횟수 (기본 80)")
    ap.add_argument("--iters-resnet-p2",type=int,   default=80,
                    help="ResNet-50 P2 반복 횟수 (기본 80)")
    ap.add_argument("--iters-vit",      type=int,   default=60,
                    help="ViT-B/16 반복 횟수 (기본 60)")
    ap.add_argument("--iters-effnet",   type=int,   default=100,
                    help="EfficientNet-B0 반복 횟수 (기본 100)")
    args = ap.parse_args()

    print("=" * 65)
    print("KRAKEN 시공간 공유 — 실제 모델 실측 시각화")
    print("  P1 (SM 0~50%) : LLM (GPT-2 style)  +  ResNet-50")
    print("  P2 (SM 50~100%): ResNet-50  +  ViT-B/16  +  EfficientNet-B0")
    print("=" * 65)

    # MPS 데몬 확인
    import subprocess as _sp
    r = _sp.run(["nvidia-cuda-mps-control", "get_server_list"],
                capture_output=True, timeout=5)
    if r.returncode != 0:
        print("[MPS] MPS 데몬 시작 중…")
        _sp.run(["nvidia-cuda-mps-control", "-d"], check=True, timeout=10)
        time.sleep(1.0)
    else:
        print("[MPS] MPS 데몬 실행 중")

    timeline, traces, t_start_abs, all_cfg = run_experiment(
        interval        = args.interval,
        warmup          = args.warmup,
        iters_llm       = args.iters_llm,
        iters_resnet_p1 = args.iters_resnet_p1,
        iters_resnet_p2 = args.iters_resnet_p2,
        iters_vit       = args.iters_vit,
        iters_effnet    = args.iters_effnet,
    )

    plot_all(timeline, traces, t_start_abs, all_cfg)

    print("\n완료!")
