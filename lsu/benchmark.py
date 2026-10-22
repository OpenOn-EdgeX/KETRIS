#!/usr/bin/env python3
# benchmark.py
# LSU(Logical Slice Unit) 실측 벤치마크
#
# PROJECT_CONTEXT 3장:
#   "1 LSU = 기준 워크로드(ResNet50 inference, FP16) 초당 처리량의 1단위"
#   예시(잠정): A100=100, RTX6000Pro=65, Warboy=18
#   → 실제 장비(RTX A6000 x2) 실측값으로 대체 필요.
#
# 이 스크립트는 device 별 ResNet50 FP16 추론 throughput(img/s)을 측정하고,
# anchor(1 LSU 당 img/s)를 기준으로 정수 LSU 값으로 환산한다.
#
# anchor 결정 방식 2가지:
#   1) --unit-img-per-s X   : 1 LSU = X img/s (절대 단위, 장비 독립)
#   2) --calibrate NAME=LSU  : 측정 장비가 지정 LSU가 되도록 unit을 역산
#                              (예: --calibrate A6000=65 → 잠정 예시값 보정)
#                              역산된 unit을 출력 → 다른 장비는 동일 절대 unit으로 측정.
#
# 출력: lsu/results/lsu_benchmark_{timestamp}.json
#   {device, throughput_img_s, latency_ms, lsu, unit_img_per_s, config...}
#
# 사용:
#   python3 benchmark.py                      # 모든 가시 GPU, A6000=65 보정
#   python3 benchmark.py --unit-img-per-s 50  # 1 LSU = 50 img/s 절대 기준
#   python3 benchmark.py --gpus 0 --batch 32 --iters 200

from __future__ import annotations

import argparse
import json
import platform
import time
from datetime import datetime
from pathlib import Path

import torch
from torchvision.models import resnet50


RESULTS_DIR = Path(__file__).parent / "results"


def measure_device(
    gpu_id: int,
    batch: int,
    iters: int,
    warmup: int,
    dtype: torch.dtype,
) -> dict:
    """단일 GPU에서 ResNet50 추론 throughput을 측정한다."""
    torch.cuda.set_device(gpu_id)
    device = torch.device(f"cuda:{gpu_id}")
    name = torch.cuda.get_device_name(gpu_id)

    # steady-state cuDNN 알고리즘 선택 (고정 입력 크기 → 최적 커널 캐싱)
    torch.backends.cudnn.benchmark = True

    model = resnet50(weights=None).to(device).to(dtype).eval()
    x = torch.randn(batch, 3, 224, 224, device=device, dtype=dtype)

    # 워밍업: 콜드스타트/cuDNN 탐색/클럭 램프업 제거
    # (NPU PoC 3가설 중 '워밍업 오버헤드'와 동일 맥락 — GPU도 동일하게 통제)
    with torch.inference_mode():
        for _ in range(warmup):
            model(x)
        torch.cuda.synchronize(device)

        # 측정: CUDA Event 기반 GPU-side 타이밍 (CPU 측정 오차 배제)
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iters):
            model(x)
        end.record()
        torch.cuda.synchronize(device)

    elapsed_ms = start.elapsed_time(end)          # 총 GPU 실행시간 (ms)
    total_imgs = batch * iters
    throughput = total_imgs / (elapsed_ms / 1000.0)  # img/s
    latency_ms = elapsed_ms / iters                  # batch당 지연

    return {
        "gpu_id": gpu_id,
        "device": name,
        "throughput_img_s": round(throughput, 1),
        "latency_ms_per_batch": round(latency_ms, 3),
        "batch": batch,
        "iters": iters,
        "warmup": warmup,
        "dtype": str(dtype).replace("torch.", ""),
    }


def resolve_unit(
    measurements: list[dict],
    unit_img_per_s: float | None,
    calibrate: str | None,
) -> tuple[float, str]:
    """1 LSU 당 img/s(unit)와 그 출처 설명을 결정한다."""
    if unit_img_per_s is not None:
        return unit_img_per_s, f"absolute (1 LSU = {unit_img_per_s} img/s)"

    if calibrate:
        # 형식: NAME=LSU  (NAME은 device 이름 부분 문자열, 대소문자 무시)
        name_part, _, lsu_str = calibrate.partition("=")
        target_lsu = float(lsu_str)
        name_part = name_part.strip().lower()
        match = next(
            (m for m in measurements if name_part in m["device"].lower()),
            None,
        )
        if match is None:
            raise SystemExit(
                f"--calibrate 장비 '{name_part}'를 측정 결과에서 찾지 못함. "
                f"측정된 장비: {[m['device'] for m in measurements]}"
            )
        unit = match["throughput_img_s"] / target_lsu
        return unit, (
            f"calibrated ({match['device']} = {target_lsu} LSU "
            f"@ {match['throughput_img_s']} img/s)"
        )

    raise SystemExit("unit_img_per_s 또는 calibrate 중 하나는 지정되어야 함")


def main():
    ap = argparse.ArgumentParser(prog="lsu-benchmark")
    ap.add_argument("--gpus", type=int, nargs="*", default=None,
                    help="측정할 GPU index 목록 (기본: 모든 가시 GPU)")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--fp32", action="store_true",
                    help="FP32로 측정 (기본 FP16)")
    ap.add_argument("--unit-img-per-s", type=float, default=None,
                    help="1 LSU = 이 img/s (절대 단위). 지정 시 --calibrate 무시")
    ap.add_argument("--calibrate", type=str, default="A6000=65",
                    help="NAME=LSU 형식. 해당 장비가 지정 LSU가 되도록 unit 역산 "
                         "(기본: A6000=65, PROJECT_CONTEXT 잠정 예시값)")
    args = ap.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("CUDA 사용 불가 — LSU 벤치는 GPU가 필요합니다.")

    dtype = torch.float32 if args.fp32 else torch.float16
    gpu_ids = args.gpus if args.gpus is not None else list(range(torch.cuda.device_count()))

    print(f"[lsu] torch={torch.__version__} cuda={torch.version.cuda} "
          f"dtype={str(dtype).replace('torch.','')} "
          f"batch={args.batch} iters={args.iters} warmup={args.warmup}")
    print(f"[lsu] 기준 워크로드: ResNet50 inference (PROJECT_CONTEXT 3장)")

    measurements = []
    for gid in gpu_ids:
        print(f"[lsu] GPU{gid} 측정 중...")
        m = measure_device(gid, args.batch, args.iters, args.warmup, dtype)
        print(f"[lsu] GPU{gid} {m['device']}: "
              f"{m['throughput_img_s']} img/s  "
              f"({m['latency_ms_per_batch']} ms/batch)")
        measurements.append(m)

    unit, unit_desc = resolve_unit(measurements, args.unit_img_per_s, args.calibrate)

    for m in measurements:
        m["lsu"] = round(m["throughput_img_s"] / unit, 1)
        m["lsu_int"] = max(1, round(m["throughput_img_s"] / unit))

    print(f"\n[lsu] unit = {unit:.2f} img/s per LSU  [{unit_desc}]")
    print(f"[lsu] {'device':28s} {'img/s':>10s} {'LSU':>8s} {'LSU(int)':>9s}")
    for m in measurements:
        print(f"[lsu] {m['device']:28s} {m['throughput_img_s']:>10.1f} "
              f"{m['lsu']:>8.1f} {m['lsu_int']:>9d}")

    out = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "host": platform.node(),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "reference_workload": "resnet50_inference",
        "dtype": str(dtype).replace("torch.", ""),
        "unit_img_per_s": round(unit, 2),
        "unit_source": unit_desc,
        "config": {"batch": args.batch, "iters": args.iters, "warmup": args.warmup},
        "measurements": measurements,
        "note": "LSU 값은 잠정. 기관 기술회의 확정 전까지 provisional.",
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"lsu_benchmark_{ts}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    latest = RESULTS_DIR / "lsu_latest.json"
    latest.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n[lsu] 저장: {path}")
    print(f"[lsu] 최신:  {latest}")


if __name__ == "__main__":
    main()
