#!/usr/bin/env python3
"""
KRAKEN 시공간 공유 시각화 — 실제 모델 실측
==========================================
실제 GPT-2 / ResNet-50 / BERT-base 모델을 KRAKEN 게이팅 하에 실행.
배리어 메커니즘으로 모델 로드 후 동시 시작 보장.

시나리오:
  (a) 시간 분할 2-way : GPT-2 (weight=2) + ResNet-50 (weight=1)   → 67%/33%
  (b) 시간 분할 3-way : GPT-2 (w=3) + ResNet-50 (w=2) + BERT (w=1) → 50%/33%/17%
  (c) 시공간 2+2      : P1(GPT-2:ResNet=2:1) | P2(ResNet:BERT=2:1), MPS=50%
  (d) 시공간 2+3      : P1(GPT-2+ResNet+BERT, 3:2:1) | P2(ResNet×3, 1:1:1), MPS=50%

사용:
  cd kraken/tests
  python3 visualize_real.py [--interval S] [--warmup S]
"""
import sys, os, time, subprocess, threading, tempfile
from pathlib import Path

ROOT  = Path(__file__).parent.parent
TESTS = Path(__file__).parent
LIB   = ROOT / "runtime" / "gating_lib.so"
OUT   = TESTS / "results"
OUT.mkdir(exist_ok=True)

WL = {
    "GPT2":   TESTS / "workload_gpt2.py",
    "ResNet": TESTS / "workload_resnet.py",
    "BERT":   TESTS / "workload_bert.py",
}
# 각 모델별 적정 iter 수 (배리어 후 ~20s 유지되도록)
# GPT-2: 10.8ms/iter, ResNet: 21.3ms/iter, BERT: 6.8ms/iter
# 2:1 weight → GPT-2 67% / ResNet 33%: GPT-2≈20s*0.67/0.0108≈1240, ResNet≈20s*0.33/0.021≈314
DEFAULT_ITERS = {
    "GPT2":   1200,
    "ResNet":  300,
    "BERT":   1000,
}

sys.path.insert(0, str(ROOT / "shm"))
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "monitor"))

from kraken_shm import create_shm, GATE_RUNNING, MODE_OVERCOMMIT
from registry  import Registry
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
plt.rcParams.update({"figure.dpi": 150, "font.size": 10,
                      "axes.titlesize": 11, "axes.spines.top": False,
                      "axes.spines.right": False})

C = {
    "GPT2":   "#E74C3C",   # 빨강
    "ResNet": "#3498DB",   # 파랑
    "BERT":   "#27AE60",   # 초록
    "ResNetB":"#F39C12",   # 주황 (P2의 ResNet 구분용)
    "BERTB":  "#8E44AD",   # 보라
    "idle":   "#EEEEEE",
}

PHYSICAL_SM = 84

# ── 유틸 ─────────────────────────────────────────────────────────────────────
def shm_cleanup(*paths):
    for p in paths:
        if os.path.exists(p): os.unlink(p)

def make_env(extra: dict) -> dict:
    e = os.environ.copy()
    e["LD_PRELOAD"] = str(LIB)
    e.update(extra)
    return e

def preinit(shm, n):
    for i in range(n):
        w = shm.policy.weights[i]
        tot = sum(shm.policy.weights[j] for j in range(n)) or 1.0
        shm.tenants[i].time_remain_us = int(shm.policy.round_duration_us * w / tot)
        shm.tenants[i].gate_state = GATE_RUNNING

def make_logger(shm, mm, interval):
    lg = MetricsLogger.__new__(MetricsLogger)
    lg.shm = shm; lg._mm = mm
    lg.interval = interval; lg.stdout = False
    lg.output = "/dev/null"; lg._prev = {}
    return lg

def wait_barrier(barrier_dir: str, ready_files: list, timeout=30.0):
    """모든 ready 파일이 생성될 때까지 대기한 후 barrier 파일 생성."""
    t0 = time.monotonic()
    while True:
        if all(os.path.exists(f) for f in ready_files):
            break
        if time.monotonic() - t0 > timeout:
            print(f"  [배리어] 타임아웃 ({timeout}s) — 준비된 프로세스만 진행")
            break
        time.sleep(0.1)
    barrier = os.path.join(barrier_dir, "barrier_start")
    open(barrier, "w").close()
    print(f"  [배리어] 모든 모델 로드 완료 → 동시 시작!")
    return barrier

def collect(shm_name, tenants_cfg, barrier_suffix,
            interval=0.5, warmup=2.0, iters_override=None):
    """
    tenants_cfg: [{"name": str, "model": str, "weight": float, "mps": int,
                   "iters": int (optional)}]
    반환: timeline list
    """
    SHM_PATH = f"/dev/shm/kraken_{shm_name}"
    shm_cleanup(SHM_PATH)

    shm, mm = create_shm(shm_name, physical_sm=PHYSICAL_SM, physical_mem_mb=49140)
    reg = Registry(shm)

    total_w = sum(t["weight"] for t in tenants_cfg)
    for tc in tenants_cfg:
        reg.register(tc["name"], virtual_sm=PHYSICAL_SM,
                     virtual_mem_mb=8192, weight=tc["weight"])
    for i, tc in enumerate(tenants_cfg):
        shm.policy.weights[i] = tc["weight"] / total_w
    shm.policy.version += 1
    preinit(shm, len(tenants_cfg))

    mps_list = [tc.get("mps", 100) for tc in tenants_cfg]
    print(f"  mode={shm.mode}  tenants: " +
          "  ".join(f"{tc['name']}(w={tc['weight']:.0f}, mps={mps_list[i]}%)"
                    for i, tc in enumerate(tenants_cfg)))

    # ── 배리어 파일 준비 ───────────────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix=f"kraken_{barrier_suffix}_")
    ready_files = [os.path.join(tmpdir, f"ready_{tc['name']}") for tc in tenants_cfg]
    barrier_file = os.path.join(tmpdir, "barrier_start")

    # ── 프로세스 기동 ─────────────────────────────────────────────────────────
    procs = []
    for i, tc in enumerate(tenants_cfg):
        iters = iters_override.get(tc["name"], DEFAULT_ITERS.get(tc["model"], 500)) \
                if iters_override else DEFAULT_ITERS.get(tc["model"], 500)
        e = make_env({
            "KRAKEN_TENANT":  tc["name"],
            "KRAKEN_GROUP":   shm_name,
            "WORKLOAD_ITERS": str(iters),
            "WORKLOAD_READY": ready_files[i],
            "WORKLOAD_BARRIER": barrier_file,
            "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_list[i]),
        })
        wl_path = WL[tc["model"]]
        procs.append(subprocess.Popen([sys.executable, str(wl_path)], env=e,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL))

    # ── 배리어: 모든 모델 로드 대기 ───────────────────────────────────────────
    print(f"  모델 로드 대기 중 (최대 60s)…")
    wait_barrier(tmpdir, ready_files, timeout=60.0)

    # ── 워밍업 ────────────────────────────────────────────────────────────────
    lg = make_logger(shm, mm, interval)
    t_start = time.monotonic()
    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup)
    lg.snapshot()  # prev 초기화

    # ── 측정 루프 ─────────────────────────────────────────────────────────────
    print("  측정 중…")
    timeline = []
    while any(p.poll() is None for p in procs):
        rows = lg.snapshot()
        ts   = round(time.monotonic() - t_start - warmup, 2)
        if rows and ts > 0:
            d = {r["tenant"]: r for r in rows}
            names = [tc["name"] for tc in tenants_cfg]
            if all(n in d for n in names):
                row = {"ts": ts}
                for n in names:
                    row[f"{n}_exec"] = d[n]["exec_rate_pct"]
                    row[f"{n}_wait"] = d[n]["wait_rate_pct"]
                    row[f"{n}_fair"] = d[n]["fairness"]
                timeline.append(row)
                line = "  ".join(f"{n}={d[n]['exec_rate_pct']:5.1f}%" for n in names)
                print(f"    t={ts:5.1f}s  {line}")
        time.sleep(interval)

    for p in procs: p.wait(timeout=30)

    # 누적 비율 출력
    total_exec = sum(shm.tenants[i].total_exec_us for i in range(len(tenants_cfg))) or 1
    print("  최종 누적 exec 비율:")
    for i, tc in enumerate(tenants_cfg):
        r = shm.tenants[i].total_exec_us / total_exec
        print(f"    {tc['name']}: {r:.3f}  (목표 {tc['weight']/total_w:.3f})")

    shm_cleanup(SHM_PATH)
    import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"  완료. 샘플={len(timeline)}")
    return timeline


def collect_spatial(n_per_partition, p1_cfg, p2_cfg, barrier_suffix,
                    interval=0.5, warmup=2.0, iters_override=None):
    """두 파티션 SHM을 동시에 운용."""
    MPS = 50
    SHM1, SHM2 = f"vr_{barrier_suffix}_p1", f"vr_{barrier_suffix}_p2"
    P1 = f"/dev/shm/kraken_{SHM1}"; P2 = f"/dev/shm/kraken_{SHM2}"
    shm_cleanup(P1, P2)

    PART_SM = PHYSICAL_SM // 2  # 42

    def mk_shm(name, cfg_list):
        shm, mm = create_shm(name, physical_sm=PART_SM, physical_mem_mb=24000)
        reg = Registry(shm)
        tw = sum(tc["weight"] for tc in cfg_list)
        for tc in cfg_list:
            reg.register(tc["name"], virtual_sm=PART_SM,
                         virtual_mem_mb=7000, weight=tc["weight"])
        for i, tc in enumerate(cfg_list):
            shm.policy.weights[i] = tc["weight"] / tw
        shm.policy.version += 1
        preinit(shm, len(cfg_list))
        return shm, mm

    shm1, mm1 = mk_shm(SHM1, p1_cfg)
    shm2, mm2 = mk_shm(SHM2, p2_cfg)

    print(f"\n[{barrier_suffix}] P1 mode={shm1.mode}  P2 mode={shm2.mode}  MPS={MPS}%")

    tmpdir = tempfile.mkdtemp(prefix=f"kraken_{barrier_suffix}_")
    all_cfg = p1_cfg + p2_cfg
    ready_files = [os.path.join(tmpdir, f"ready_{tc['name']}") for tc in all_cfg]
    barrier_file = os.path.join(tmpdir, "barrier_start")

    procs = []
    ridx = 0
    for grp_name, grp_cfg in [(SHM1, p1_cfg), (SHM2, p2_cfg)]:
        tw = sum(tc["weight"] for tc in grp_cfg)
        for tc in grp_cfg:
            iters = iters_override.get(tc["name"],
                    DEFAULT_ITERS.get(tc["model"], 500)) if iters_override \
                    else DEFAULT_ITERS.get(tc["model"], 500)
            e = make_env({
                "KRAKEN_TENANT":   tc["name"],
                "KRAKEN_GROUP":    grp_name,
                "WORKLOAD_ITERS": str(iters),
                "WORKLOAD_READY": ready_files[ridx],
                "WORKLOAD_BARRIER": barrier_file,
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(MPS),
            })
            procs.append(subprocess.Popen([sys.executable, str(WL[tc["model"]])],
                                           env=e, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL))
            ridx += 1

    print("  모델 로드 대기 중 (최대 60s)…")
    wait_barrier(tmpdir, ready_files, timeout=60.0)

    lg1 = make_logger(shm1, mm1, interval)
    lg2 = make_logger(shm2, mm2, interval)

    _buf1, _buf2 = {}, {}
    lock = threading.Lock()

    def poll_p1():
        time.sleep(warmup); lg1.snapshot()
        while any(p.poll() is None for p in procs):
            rows = lg1.snapshot()
            with lock:
                for r in (rows or []): _buf1[r["tenant"]] = r
            time.sleep(interval)

    def poll_p2():
        time.sleep(warmup); lg2.snapshot()
        while any(p.poll() is None for p in procs):
            rows = lg2.snapshot()
            with lock:
                for r in (rows or []): _buf2[r["tenant"]] = r
            time.sleep(interval)

    th1 = threading.Thread(target=poll_p1, daemon=True)
    th2 = threading.Thread(target=poll_p2, daemon=True)
    th1.start(); th2.start()

    t_start = time.monotonic()
    print(f"  워밍업 {warmup}s…")
    time.sleep(warmup + interval)

    print("  측정 중…")
    timeline = []
    p1_names = [tc["name"] for tc in p1_cfg]
    p2_names = [tc["name"] for tc in p2_cfg]

    while any(p.poll() is None for p in procs):
        ts = round(time.monotonic() - t_start - warmup, 2)
        with lock:
            b1, b2 = dict(_buf1), dict(_buf2)
        if ts > 0 and b1 and b2:
            row = {"ts": ts}
            for n in p1_names: row[f"{n}_exec"] = b1.get(n, {}).get("exec_rate_pct", 0.0)
            for n in p2_names: row[f"{n}_exec"] = b2.get(n, {}).get("exec_rate_pct", 0.0)
            timeline.append(row)
            s1 = "  ".join(f"{n}={row[f'{n}_exec']:5.1f}%" for n in p1_names)
            s2 = "  ".join(f"{n}={row[f'{n}_exec']:5.1f}%" for n in p2_names)
            print(f"    t={ts:5.1f}s  P1[{s1}]  P2[{s2}]")
        time.sleep(interval)

    for p in procs: p.wait(timeout=30)
    shm_cleanup(P1, P2)
    import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"  완료. 샘플={len(timeline)}")
    return timeline, p1_names, p2_names


# ── Plot: 시간 분할 타임라인 (상세) ──────────────────────────────────────────
def plot_temporal_detail(timeline, tenants_cfg, title, fname):
    if not timeline: print(f"[{fname}] 데이터 없음"); return
    ts = np.array([d["ts"] for d in timeline])
    names  = [tc["name"]   for tc in tenants_cfg]
    labels = [tc.get("label", tc["name"]) for tc in tenants_cfg]
    weights = [tc["weight"] for tc in tenants_cfg]
    total_w = sum(weights)

    n = len(names)
    fig, axes = plt.subplots(n + 1, 1, figsize=(12, 3.5*(n+1)),
                              sharex=True,
                              gridspec_kw={"height_ratios": [3]*n + [2]})
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.01)

    for i, (name, label) in enumerate(zip(names, labels)):
        ax = axes[i]
        ex = np.array([d.get(f"{name}_exec", 0) for d in timeline])
        wa = np.array([d.get(f"{name}_wait", 0) for d in timeline])
        target = weights[i] / total_w * 100
        ax.fill_between(ts, ex, alpha=0.8, color=C.get(name, "#888"),
                        label="exec", linewidth=0)
        ax.fill_between(ts, wa, alpha=0.35, color="#BBB", label="wait", linewidth=0)
        ax.axhline(target, ls="--", lw=1.4, color=C.get(name, "#888"), alpha=0.65,
                   label=f"목표 {target:.0f}%")
        ax.set_ylabel("비율 (%)")
        ax.set_ylim(0, 115)
        ax.set_title(f"{label}  (weight={weights[i]:.0f})", loc="left", fontsize=11)
        ax.legend(loc="upper right", framealpha=0.75, fontsize=9)
        ax.grid(axis="y", alpha=0.2)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))

    ax_f = axes[-1]
    for name in names:
        fair = np.array([d.get(f"{name}_fair", 1) for d in timeline])
        ax_f.plot(ts, fair, lw=1.8, color=C.get(name, "#888"), label=name)
    ax_f.axhline(1.0, ls="--", lw=1.2, color="#555", alpha=0.6)
    ax_f.set_ylabel("Fairness"); ax_f.set_xlabel("경과 시간 (s)")
    ax_f.set_ylim(0, 2.5)
    ax_f.set_title("Fairness (실제/목표, 1.0=이상)", loc="left", fontsize=11)
    ax_f.legend(loc="upper right", framealpha=0.75, fontsize=9, ncol=n)
    ax_f.grid(axis="y", alpha=0.2)

    plt.tight_layout(rect=[0, 0, 1, 0.99])
    path = OUT / fname
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] 저장: {path}")


# ── Plot: 시공간 분할 다이어그램 ─────────────────────────────────────────────
def plot_st_detail(timeline, p1_cfg, p2_cfg, title, fname):
    if not timeline: print(f"[{fname}] 데이터 없음"); return

    p1_names  = [tc["name"] for tc in p1_cfg]
    p2_names  = [tc["name"] for tc in p2_cfg]
    all_names = p1_names + p2_names
    color_map = {}
    for tc in p1_cfg + p2_cfg:
        color_map[tc["name"]] = tc.get("color", C.get(tc["name"], "#888"))

    ts = np.array([d["ts"] for d in timeline])
    dt = float(np.mean(np.diff(ts))) if len(ts) > 1 else 0.5

    fig, (ax_main, ax_eff) = plt.subplots(
        2, 1, figsize=(14, 9),
        gridspec_kw={"height_ratios": [4, 1.8]}, sharex=True)
    fig.suptitle(title, fontsize=13, fontweight="bold", y=1.01)

    P1_LO, P1_HI = 0,  50
    P2_LO, P2_HI = 50, 100

    for i, row in enumerate(timeline):
        x_lo = ts[i] - dt * 0.45
        x_hi = ts[i] + dt * 0.45

        for (lo, hi, names) in [(P1_LO, P1_HI, p1_names),
                                  (P2_LO, P2_HI, p2_names)]:
            execs = np.array([row.get(f"{n}_exec", 0.0) for n in names])
            total = execs.sum() or 1.0
            fracs = execs / total
            used  = min(1.0, execs.sum() / 100.0)
            cur   = lo
            for j, n in enumerate(names):
                h = fracs[j] * used * (hi - lo)
                if h > 0.05:
                    ax_main.fill_between([x_lo, x_hi], [cur, cur], [cur+h, cur+h],
                                         color=color_map[n], alpha=0.85, linewidth=0)
                cur += h
            if cur < hi:
                ax_main.fill_between([x_lo, x_hi], [cur, cur], [hi, hi],
                                     color=C["idle"], alpha=0.45, linewidth=0)

    ax_main.axhline(50, color="black", lw=2.2, zorder=5)
    ax_main.fill_betweenx([P1_HI, P2_LO], ts[0]-dt, ts[-1]+dt,
                           color="white", alpha=1.0, zorder=4)
    ax_main.text((ts[0]+ts[-1])/2, 50, "── SM 파티션 경계  (MPS=50%) ──",
                  ha="center", va="center", fontsize=8, color="#333", zorder=6)

    ax_main.set_ylim(0, 100)
    ax_main.set_ylabel("SM 공간 (%)")
    ax_main.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_main.grid(axis="y", alpha=0.2)

    for tc in p1_cfg + p2_cfg:
        tag = "(P1)" if tc["name"] in p1_names else "(P2)"
        lab = f"{tc.get('label', tc['name'])} {tag}"
        mpatches_patch = mpatches.Patch(color=color_map[tc["name"]],
                                         alpha=0.85, label=lab)
        ax_main.add_artist(mpatches_patch)
    patches = [mpatches.Patch(color=color_map[tc["name"]], alpha=0.85,
                               label=f"{tc.get('label', tc['name'])} {'P1' if tc['name'] in p1_names else 'P2'}")
               for tc in p1_cfg + p2_cfg]
    patches.append(mpatches.Patch(color=C["idle"], alpha=0.45, label="Idle"))
    ax_main.legend(handles=patches, loc="upper right", fontsize=8.5,
                   framealpha=0.85, ncol=2)

    ax_main.text(-0.09, 0.25, f"Partition 1\n(SM 0~50%)",
                  transform=ax_main.transAxes, ha="center", va="center",
                  fontsize=8.5, color="#C0392B", fontweight="bold", rotation=90)
    ax_main.text(-0.09, 0.75, f"Partition 2\n(SM 50~100%)",
                  transform=ax_main.transAxes, ha="center", va="center",
                  fontsize=8.5, color="#1E8449", fontweight="bold", rotation=90)

    # 하단: 실효 SM 점유율 라인
    for n in all_names:
        vals = np.array([d.get(f"{n}_exec", 0.0) for d in timeline]) * 0.5
        ax_eff.plot(ts, vals, lw=1.8, color=color_map[n],
                    label=n)
    ax_eff.set_ylabel("실효 SM 점유율 (%)")
    ax_eff.set_xlabel("경과 시간 (s)")
    ax_eff.set_ylim(0, 55)
    ax_eff.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_eff.legend(loc="upper right", fontsize=8, framealpha=0.8, ncol=len(all_names))
    ax_eff.grid(axis="y", alpha=0.2)

    plt.tight_layout(rect=[0, 0, 1, 0.99])
    path = OUT / fname
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] 저장: {path}")


# ── 2×2 Summary ──────────────────────────────────────────────────────────────
def plot_summary(ts2, ts3, st2, st3, p1_t2, p2_t2, p1_t3, p2_t3):
    fig = plt.figure(figsize=(19, 13))
    from matplotlib.gridspec import GridSpec
    gs = GridSpec(2, 2, figure=fig, hspace=0.50, wspace=0.35)
    fig.suptitle("KRAKEN 시공간 GPU 공유 — 실측 결과 (실제 모델)\n"
                 "GPT-2 / ResNet-50 / BERT-base  on  NVIDIA RTX A6000",
                 fontsize=13, fontweight="bold", y=1.01)

    def draw_temporal(ax, tl, cfg, subtitle):
        if not tl: return
        ts_ = np.array([d["ts"] for d in tl])
        tw = sum(tc["weight"] for tc in cfg)
        for tc in cfg:
            name = tc["name"]
            ex = np.array([d.get(f"{name}_exec", 0) for d in tl])
            target = tc["weight"] / tw * 100
            ax.fill_between(ts_, ex, alpha=0.78,
                            color=C.get(name, "#888"),
                            label=f"{tc.get('label', name)} (w={tc['weight']:.0f})",
                            linewidth=0)
            ax.axhline(target, ls="--", lw=1.1,
                       color=C.get(name, "#888"), alpha=0.55)
        ax.set_ylim(0, 110)
        ax.set_ylabel("SM 사용률 (%)")
        ax.set_xlabel("경과 시간 (s)")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        ax.legend(loc="upper right", fontsize=8.5, framealpha=0.8)
        ax.grid(axis="y", alpha=0.2)
        ax.set_title(subtitle, fontsize=10, fontweight="bold", pad=7)

    def draw_st(ax, tl, p1_names, p2_names, cfg_map, subtitle):
        if not tl: return
        ts_ = np.array([d["ts"] for d in tl])
        dt_ = float(np.mean(np.diff(ts_))) if len(ts_) > 1 else 0.5
        for i, row in enumerate(tl):
            x_lo = ts_[i] - dt_*0.45; x_hi = ts_[i] + dt_*0.45
            for (lo, hi, names) in [(0, 50, p1_names), (50, 100, p2_names)]:
                execs = np.array([row.get(f"{n}_exec", 0.0) for n in names])
                used  = min(1.0, execs.sum() / 100.0)
                fracs = execs / (execs.sum() or 1.0)
                cur   = lo
                for j, n in enumerate(names):
                    h = fracs[j] * used * (hi - lo)
                    if h > 0.05:
                        ax.fill_between([x_lo, x_hi], [cur, cur], [cur+h, cur+h],
                                        color=cfg_map.get(n, "#888"),
                                        alpha=0.85, linewidth=0)
                    cur += h
                if cur < hi:
                    ax.fill_between([x_lo, x_hi], [cur, cur], [hi, hi],
                                    color=C["idle"], alpha=0.45, linewidth=0)

        ax.axhline(50, color="black", lw=2.0, zorder=5)
        ax.fill_betweenx([50, 50], ts_[0]-dt_, ts_[-1]+dt_,
                          color="white", alpha=1.0, zorder=4)
        ax.text((ts_[0]+ts_[-1])/2, 50, "── SM 경계 ──",
                ha="center", va="center", fontsize=7, color="#444", zorder=6)

        all_n = p1_names + p2_names
        patches = [mpatches.Patch(color=cfg_map.get(n, "#888"), alpha=0.85,
                                   label=f"{n} {'P1' if n in p1_names else 'P2'}")
                   for n in all_n]
        patches.append(mpatches.Patch(color=C["idle"], alpha=0.45, label="Idle"))
        ax.legend(handles=patches, loc="upper right", fontsize=7.5,
                  framealpha=0.85, ncol=2)
        ax.set_ylim(0, 100); ax.set_ylabel("SM 공간 (%)")
        ax.set_xlabel("경과 시간 (s)")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        ax.grid(axis="y", alpha=0.2)
        ax.text(-0.09, 0.25, "P1\n0~50%", transform=ax.transAxes,
                ha="center", va="center", fontsize=8, color="#C0392B",
                fontweight="bold", rotation=90)
        ax.text(-0.09, 0.75, "P2\n50~100%", transform=ax.transAxes,
                ha="center", va="center", fontsize=8, color="#1E8449",
                fontweight="bold", rotation=90)
        ax.set_title(subtitle, fontsize=10, fontweight="bold", pad=7)

    ax_a = fig.add_subplot(gs[0, 0])
    draw_temporal(ax_a, ts2,
                  [{"name":"GPT2",   "weight":2, "label":"GPT-2 (weight=2)"},
                   {"name":"ResNet", "weight":1, "label":"ResNet-50 (weight=1)"}],
                  "(a) 시간 분할  2-way  |  GPT-2 (w=2) + ResNet-50 (w=1)\n"
                  "→ 목표 67% / 33%")

    ax_b = fig.add_subplot(gs[0, 1])
    draw_temporal(ax_b, ts3,
                  [{"name":"GPT2",   "weight":3, "label":"GPT-2 (w=3)"},
                   {"name":"ResNet", "weight":2, "label":"ResNet-50 (w=2)"},
                   {"name":"BERT",   "weight":1, "label":"BERT (w=1)"}],
                  "(b) 시간 분할  3-way  |  GPT-2 (w=3) + ResNet (w=2) + BERT (w=1)\n"
                  "→ 목표 50% / 33% / 17%")

    cfg_map_c = {"GPT2": C["GPT2"], "ResNet": C["ResNet"],
                 "GPT2_B": "#F39C12", "ResNet_B": "#8E44AD"}

    ax_c = fig.add_subplot(gs[1, 0])
    draw_st(ax_c, st2,
            [tc["name"] for tc in p1_t2],
            [tc["name"] for tc in p2_t2],
            {tc["name"]: tc.get("color", C.get(tc["name"], "#888"))
             for tc in p1_t2 + p2_t2},
            "(c) 시공간 분할  2+2  |  P1: GPT-2(w=2)+ResNet(w=1)\n"
            "P2: GPT-2(w=1)+BERT(w=2)  [각 파티션 MPS=50%]")

    ax_d = fig.add_subplot(gs[1, 1])
    draw_st(ax_d, st3,
            [tc["name"] for tc in p1_t3],
            [tc["name"] for tc in p2_t3],
            {tc["name"]: tc.get("color", C.get(tc["name"], "#888"))
             for tc in p1_t3 + p2_t3},
            "(d) 시공간 분할  2+3  |  P1: GPT-2(w=3)+ResNet(w=2)+BERT(w=1)\n"
            "P2: GPT-2(w=1)+ResNet(w=1)+BERT(w=1)  [각 파티션 MPS=50%]")

    out_path = OUT / "fig_real_summary.png"
    plt.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"\n[summary] 저장: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--warmup",   type=float, default=2.0)
    ap.add_argument("--only",     choices=["ts2","ts3","st2","st3"], default=None)
    args = ap.parse_args()

    IV = {"GPT2": args.interval, "ResNet": args.interval, "BERT": args.interval}

    print("=" * 65)
    print("KRAKEN 실제 모델 시공간 공유 실측")
    print("=" * 65)

    ts2 = ts3 = st2 = st3 = []
    p1_t2 = p2_t2 = p1_t3 = p2_t3 = ([], [])

    # ── (a) 시간 분할 2-way: GPT-2(w=2) + ResNet-50(w=1) ─────────────────────
    if args.only in (None, "ts2"):
        print("\n[ts2] GPT-2 (w=2) + ResNet-50 (w=1) → 목표 67%/33%")
        ts2 = collect("vr_ts2",
                      [{"name":"GPT2",   "model":"GPT2",   "weight":2},
                       {"name":"ResNet", "model":"ResNet", "weight":1}],
                      "ts2",
                      interval=args.interval, warmup=args.warmup,
                      iters_override={"GPT2": 1200, "ResNet": 300})
        plot_temporal_detail(
            ts2,
            [{"name":"GPT2",   "weight":2, "label":"GPT-2 small (seq=128)"},
             {"name":"ResNet", "weight":1, "label":"ResNet-50 (batch=32)"}],
            "KRAKEN 시간 분할 — 2-way\n"
            "GPT-2 (weight=2, 목표 67%) + ResNet-50 (weight=1, 목표 33%)",
            "fig_real_ts2.png")

    # ── (b) 시간 분할 3-way: GPT-2(w=3) + ResNet(w=2) + BERT(w=1) ───────────
    if args.only in (None, "ts3"):
        print("\n[ts3] GPT-2(w=3) + ResNet(w=2) + BERT(w=1) → 목표 50%/33%/17%")
        ts3 = collect("vr_ts3",
                      [{"name":"GPT2",   "model":"GPT2",   "weight":3},
                       {"name":"ResNet", "model":"ResNet", "weight":2},
                       {"name":"BERT",   "model":"BERT",   "weight":1}],
                      "ts3",
                      interval=args.interval, warmup=args.warmup,
                      iters_override={"GPT2": 1200, "ResNet": 400, "BERT": 1000})
        plot_temporal_detail(
            ts3,
            [{"name":"GPT2",   "weight":3, "label":"GPT-2 small (seq=128)"},
             {"name":"ResNet", "weight":2, "label":"ResNet-50 (batch=32)"},
             {"name":"BERT",   "weight":1, "label":"BERT-base (seq=128)"}],
            "KRAKEN 시간 분할 — 3-way\n"
            "GPT-2 (w=3, 50%) + ResNet-50 (w=2, 33%) + BERT (w=1, 17%)",
            "fig_real_ts3.png")

    # ── (c) 시공간 2+2: P1(GPT-2 w=2, ResNet w=1), P2(GPT-2 w=1, BERT w=2) ──
    if args.only in (None, "st2"):
        print("\n[st2] 시공간 2파티션 × 2워크로드")
        p1_c = [{"name":"GPT2",   "model":"GPT2",   "weight":2,
                  "color":C["GPT2"]},
                {"name":"ResNet", "model":"ResNet", "weight":1,
                  "color":C["ResNet"]}]
        p2_c = [{"name":"GPT2_B", "model":"GPT2",   "weight":1,
                  "color":"#F39C12", "label":"GPT-2 (P2)"},
                {"name":"BERT",   "model":"BERT",   "weight":2,
                  "color":C["BERT"]}]
        # name이 GPT2_B인 경우도 workload_gpt2.py 사용
        WL["GPT2_B"] = WL["GPT2"]
        st2, p1_names2, p2_names2 = collect_spatial(
            2, p1_c, p2_c, "st2",
            interval=args.interval, warmup=args.warmup,
            iters_override={"GPT2": 1000, "ResNet": 250,
                             "GPT2_B": 600, "BERT": 800})
        p1_t2 = p1_c; p2_t2 = p2_c
        plot_st_detail(
            st2, p1_c, p2_c,
            "KRAKEN 시공간 분할 — 2파티션 × 2워크로드\n"
            "P1(GPT-2 w=2, ResNet w=1) | P2(GPT-2 w=1, BERT w=2)  [MPS=50%]",
            "fig_real_st2.png")

    # ── (d) 시공간 2+3: P1(GPT-2/ResNet/BERT = 3:2:1), P2(셋 다 1:1:1) ──────
    if args.only in (None, "st3"):
        print("\n[st3] 시공간 2파티션 × 3워크로드")
        p1_d = [{"name":"GPT2",    "model":"GPT2",   "weight":3, "color":C["GPT2"]},
                {"name":"ResNet",  "model":"ResNet", "weight":2, "color":C["ResNet"]},
                {"name":"BERT",    "model":"BERT",   "weight":1, "color":C["BERT"]}]
        p2_d = [{"name":"GPT2_B",  "model":"GPT2",  "weight":1,
                  "color":"#F39C12", "label":"GPT-2 (P2)"},
                {"name":"ResNetB", "model":"ResNet", "weight":1,
                  "color":"#8E44AD", "label":"ResNet (P2)"},
                {"name":"BERTB",   "model":"BERT",   "weight":1,
                  "color":"#16A085", "label":"BERT (P2)"}]
        WL["GPT2_B"]  = WL["GPT2"]
        WL["ResNetB"] = WL["ResNet"]
        WL["BERTB"]   = WL["BERT"]
        st3, p1_names3, p2_names3 = collect_spatial(
            3, p1_d, p2_d, "st3",
            interval=args.interval, warmup=args.warmup,
            iters_override={"GPT2": 1000, "ResNet": 250, "BERT": 800,
                             "GPT2_B": 600, "ResNetB": 200, "BERTB": 600})
        p1_t3 = p1_d; p2_t3 = p2_d
        plot_st_detail(
            st3, p1_d, p2_d,
            "KRAKEN 시공간 분할 — 2파티션 × 3워크로드\n"
            "P1(GPT-2 w=3, ResNet w=2, BERT w=1) | P2(세 모델 w=1:1:1)  [MPS=50%]",
            "fig_real_st3.png")

    # ── Summary ───────────────────────────────────────────────────────────────
    if all([ts2, ts3, st2, st3]):
        plot_summary(ts2, ts3, st2, st3,
                     p1_t2, p2_t2, p1_t3, p2_t3)

    print("\n완료!")
