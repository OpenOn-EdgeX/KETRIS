#!/usr/bin/env python3
# KETI(우리) 컴포넌트 — 대체 대상 아님. 타 기관 스텁을 우리 라이브 경로에 잇는 브리지.
#
# 역할:
#  1) capability(A-1) 수신 → 카탈로그 항목 산출. measured=False면 잠정 LSU로 등재(그 상태를 API에 노출).
#  2) [폐기 — Exp_90] /capacity(A-2) :8091 서빙은 shim이었다. **exp90부터 라이브 controller(:8090)가
#     /capacity 를 직접 서빙**(controller/kraken_controller.py, Exp_82 소스 흡수 배포 완료)하므로
#     shim 불요. 소비처(exporter/submitter/demo)는 :8090/capacity 로 이관됨. 이 파일의 /capacity
#     서빙 코드는 오프라인/과거 재현용으로 남기되(라이브에선 미기동), capability 수신(역할 1)만 사용.
import json, os, subprocess, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CAP_DIR = os.environ.get("KRAKEN_CAP_DIR", "/var/lib/kraken/capability")
CTRL = os.environ.get("KRAKEN_CTRL", "http://127.0.0.1:8090")
NODE = os.environ.get("KRAKEN_NODE", "gpu-npu-server-02")
# 실측 LSU 카탈로그(레퍼런스 워크로드 실측). 미실측 장비는 여기 없음 → 잠정값.
MEASURED_LSU = {"NVIDIA RTX PRO 6000 Blackwell Server Edition": 178}
PROVISIONAL_LSU = {"npu": 8}   # NPU는 PE수=잠정 LSU(실측 전)


def ingest_capability(cap):
    """A-1 capability → 카탈로그 항목. 실측 없으면 잠정."""
    model = cap["device"]["model"]
    if cap["device"]["kind"] == "gpu" and model in MEASURED_LSU:
        return {"model": model, "lsu": MEASURED_LSU[model], "measured": True}
    # 미실측: 잠정값 + 플래그
    prov = cap.get("npu", {}).get("cores") or cap["compute"]["units"]
    return {"model": model, "lsu": prov, "measured": False, "note": "잠정값(미실측) — 레퍼런스 실측 필요"}


def kubectl_allocatable():
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "node", NODE, "-o",
             "jsonpath={.status.allocatable.keti\\.re\\.kr/lsu} {.status.allocatable.keti\\.re\\.kr/npu-pe}"],
            timeout=10).decode().split()
        return int(out[0]), int(out[1])
    except Exception:
        return 0, 0


def ctrl_status():
    try:
        with urllib.request.urlopen(CTRL + "/status", timeout=5) as r:
            return json.load(r)
    except Exception:
        return {}


def feeder_status():
    try:
        with urllib.request.urlopen(CTRL + "/feeder/status", timeout=5) as r:
            return json.load(r)
    except Exception:
        return {"tenants": {}}


def build_capacity():
    st = ctrl_status()
    fs = feeder_status()
    adv_lsu, npu_pe = kubectl_allocatable()
    phys = 178
    factor = round(adv_lsu / phys, 2) if adv_lsu else 1.0
    tenants = fs.get("tenants", {})
    allocated = sum(round(v.get("ratio", 0) * phys) for v in tenants.values())
    ratio = round(allocated / phys, 3) if phys else 0.0
    slices = [{"tenant": k, "compute_pct": round(v.get("ratio", 0) * 100, 1),
               "time_ratio": round(v.get("ratio", 0), 3), "armed": v.get("armed", False)}
              for k, v in tenants.items()]
    # capability 등재 상태(measured 플래그) 반영
    caps = []
    if os.path.isdir(CAP_DIR):
        for fn in os.listdir(CAP_DIR):
            if fn.endswith(".json"):
                caps.append(ingest_capability(json.load(open(os.path.join(CAP_DIR, fn)))))
    gpu_cap = next((c for c in caps if c["measured"]), {"measured": True})
    return {
        "schema_version": "1.0", "node": NODE,
        "devices": [{
            "uuid": st.get("group_id", "GPU-default"), "kind": "gpu",
            "model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "capacity": {"physical_lsu": phys, "advertised_lsu": adv_lsu, "overcommit_factor": factor,
                         "allocated_lsu": allocated, "allocation_ratio": ratio,
                         "lsu_measured": gpu_cap.get("measured", True)},
            "memory": {"capacity_mb": st.get("physical_mem_mb", 97887), "quota_divisor": "advertised_lsu"},
            "slices": slices,
            "interference_hint": {"current_sigma_pct": ratio, "no_interference_below": 1.00,
                                  "interference_above": 1.40,
                                  "note": "100~140%는 victim 무게 의존 전이(Exp_80). 경량 저지연 victim ~130%까지 안전"},
        }],
        "npu": {"available_cores": npu_pe,
                "request_rule": {"allowed_blocks": [1, 2, 4, 8], "alignment": "power_of_two_contiguous"},
                "lsu_measured": False},
        "provisional_devices": [c for c in caps if not c["measured"]],
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/capacity":
            body = json.dumps(build_capacity(), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("KRAKEN_CAPACITY_PORT", "8091"))
    print(f"[keti-bridge] /capacity on :{port} (라이브 controller {CTRL} 집계)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
