#!/usr/bin/env python3
# 이 컴포넌트는 하스퍼 기관 구현으로 대체됩니다. 인터페이스(A-1) 검증용 최소 스텁입니다.
#
# 최소 범위: 232의 실제 GPU/NPU 정보를 읽어 A-1 capability JSON을 파일로 드롭한다.
# 하지 않는 것: PCIe/CXL 통신, fabric 발견, 실제 회수 하드웨어 제어(status 필드로 신호만).
import json, os, subprocess, sys, time

CAP_DIR = os.environ.get("KRAKEN_CAP_DIR", "/var/lib/kraken/capability")


def gpu_capability():
    q = "nvidia-smi --query-gpu=uuid,name,memory.total,clocks.max.sm --format=csv,noheader,nounits"
    out = subprocess.check_output(q.split()).decode().strip().splitlines()[0]
    uuid, name, mem_mb, _clk = [x.strip() for x in out.split(",")]
    # SM 수는 controller /status(물리 실측)에서. 없으면 하스퍼가 스펙시트로 채움.
    sm = 188  # RTX PRO 6000 Blackwell (controller physical_sm와 일치)
    return {
        "schema_version": "1.0",
        "device": {"uuid": uuid, "kind": "gpu", "model": name, "vendor": "nvidia", "remote": False},
        "compute": {"units": sm, "unit_kind": "sm",
                    "concurrency": {"mps": True, "mig": False, "partition_granularity_pct": 1}},
        "memory": {"capacity_mb": int(float(mem_mb))},
        "status": os.environ.get("KRAKEN_CAP_STATUS", "allocatable"),
        "measured": True,          # 이 장비는 KETI 실측 LSU 존재(A6000 교정 승계)
        "extra": {"driver": "590.48.01", "cuda": "13.1"},
    }


def npu_capability():
    # RNGD: PE 8, 융합 블록 {1,2,4,8} (Exp_68 검증). 실측 LSU 없음 → measured=False(잠정).
    return {
        "schema_version": "1.0",
        "device": {"uuid": "NPU-rngd-npu1", "kind": "npu", "model": "FuriosaAI RNGD", "vendor": "furiosa", "remote": False},
        "compute": {"units": 8, "unit_kind": "pe", "concurrency": {"mps": False, "mig": False}},
        "memory": {"capacity_mb": 8 * 6082},
        "npu": {"cores": 8, "mem_per_core_mb": 6082,
                "fusion": {"allowed_blocks": [1, 2, 4, 8], "alignment": "power_of_two_contiguous"}},
        "status": "allocatable",
        "measured": False,         # ★ 미실측 → KETI가 잠정 LSU로 등재해야 함
        "extra": {"pe_range": "PE0-7"},
    }


def main():
    os.makedirs(CAP_DIR, exist_ok=True)
    for cap in (gpu_capability(), npu_capability()):
        path = os.path.join(CAP_DIR, cap["device"]["uuid"] + ".json")
        with open(path, "w") as f:
            json.dump(cap, f, indent=2)
        print(f"[hasper-stub] dropped {path} (status={cap['status']}, measured={cap['measured']})")


if __name__ == "__main__":
    main()
