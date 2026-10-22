#!/usr/bin/env python3
# 이 컴포넌트는 오케스트로 기관 구현으로 대체됩니다. 인터페이스(A-4) 검증용 최소 스텁입니다.
#
# 최소 범위: 라이브 controller/feeder + 노드 allocatable을 A-4 Prometheus 노출 형식으로 /metrics 서빙.
# 하지 않는 것: Prometheus 서버, Grafana, 대시보드 UX, 알람. exporter 엔드포인트까지만.
import json, os, re, subprocess, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CTRL = os.environ.get("KRAKEN_CTRL", "http://127.0.0.1:8090")
NODE = os.environ.get("KRAKEN_NODE", "gpu-npu-server-02")
CAP_API = os.environ.get("KRAKEN_CAPACITY_API", "http://127.0.0.1:8090/capacity")


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception:
        return {}


def render():
    cap = _get(CAP_API)
    fs = _get(CTRL + "/feeder/status")
    if not cap.get("devices"):
        return "# capacity API 미응답\n"
    d = cap["devices"][0]; c = d["capacity"]
    ratio = c["allocation_ratio"]
    zone = 2 if ratio >= 1.40 else (1 if ratio >= 1.00 else 0)
    L = f'node="{NODE}",device="{d["uuid"]}",location="local"'   # [Exp_91] 로컬/원격 구분
    out = [
        "# HELP kraken_node_allocation_ratio 총 할당률(=Σpct, 간섭 축)", "# TYPE kraken_node_allocation_ratio gauge",
        f"kraken_node_physical_lsu{{{L}}} {c['physical_lsu']}",
        f"kraken_node_advertised_lsu{{{L}}} {c['advertised_lsu']}",
        f"kraken_node_overcommit_factor{{{L}}} {c['overcommit_factor']}",
        f"kraken_node_allocation_ratio{{{L}}} {ratio}",
        f"kraken_node_interference_zone{{{L}}} {zone}",
        f'kraken_node_npu_cores_available{{node="{NODE}"}} {cap["npu"]["available_cores"]}',
    ]
    mode = fs.get("env_policy", "strict")
    ret = _retention()          # [Exp_82] 워크로드 결과 → 지연민감 유지율
    for s in d.get("slices", []):
        cls = "memory" if not s["armed"] else "compute"
        SL = f'{L},tenant="{s["tenant"]}",class="{cls}"'
        out += [
            f"kraken_slice_compute_pct{{{SL}}} {s['compute_pct']}",
            f"kraken_slice_time_ratio{{{SL}}} {s['time_ratio']}",
            f"kraken_slice_armed{{{SL}}} {1 if s['armed'] else 0}",
            f'kraken_slice_control_mode{{{SL},mode="{mode}"}} 1',
        ]
    for pod, r in ret.items():
        out.append(f'kraken_slice_retention_ratio{{{L},pod="{pod}",class="memory"}} {r}')
    # [Exp_83] NPU PE 점유 맵 (furiosa-smi ps 실점유 — 이기종 가시화)
    npu = cap.get("npu", {})
    out.append(f'kraken_npu_cores_available{{node="{NODE}"}} {npu.get("available_cores", 0)}')
    # [Exp_91] 원격 풀(RemotePool CR) — location="remote" 로 로컬과 구분
    try:
        rp = subprocess.run(["kubectl", "get", "remotepools", "-o", "json"],
                            capture_output=True, text=True, timeout=5)
        for p in json.loads(rp.stdout).get("items", []):
            dev = p["spec"]["device"]; st = p.get("status", {})
            RL = f'node="{NODE}",device="{dev["uuid"]}",location="remote",provider="{p["spec"].get("providerHost","")}"'
            out.append(f'kraken_node_advertised_lsu{{{RL}}} {st.get("advertisedLsu", 0)}')
            out.append(f'kraken_remote_leased_lsu{{{RL}}} {st.get("leasedLsu", 0)}')
            out.append(f'kraken_remote_pool_phase{{{RL},phase="{st.get("phase","")}"}} 1')
    except Exception:
        pass
    for rng in _npu_blocks():
        out.append(f'kraken_npu_pe_block{{node="{NODE}",pe_range="{rng}"}} 1')
    return "\n".join(out) + "\n"


FURIOSA_SMI = os.environ.get("FURIOSA_SMI", "/usr/bin/furiosa-smi")


def _npu_blocks():
    """호스트 furiosa-smi ps → 점유 중인 PE 블록 목록(예 ['0-3','4-7'])."""
    blocks = []
    try:
        out = subprocess.check_output([FURIOSA_SMI, "ps"], timeout=5).decode()
        for line in out.splitlines():
            m = re.search(r"npu\d+:\[(\d+),\s*(\d+)\]", line)
            if m:
                blocks.append(f"{m.group(1)}-{m.group(2)}")
    except Exception:
        pass
    return blocks


# [Exp_82] 데모 결과 디렉터리의 result_*.json → 유지율(sps÷solo). solo 천장 Exp_80.
RESULT_DIR = os.environ.get("KRAKEN_RESULT_DIR", "/root/kraken-demo-sync")
SOLO = {1: 138.4, 8: 83.2, 16: 41.1, 32: 26.0}


def _retention():
    out = {}
    if not os.path.isdir(RESULT_DIR):
        return out
    for fn in os.listdir(RESULT_DIR):
        if not (fn.startswith("result_") and fn.endswith(".json")):
            continue
        try:
            r = json.load(open(os.path.join(RESULT_DIR, fn)))
            solo = SOLO.get(r.get("batch"))
            if solo and r.get("sps"):
                out[fn[len("result_"):-len(".json")]] = round(r["sps"] / solo, 3)
        except Exception:
            continue
    return out


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/metrics":
            body = render().encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("KRAKEN_EXPORTER_PORT", "9095"))
    print(f"[orchestro-stub] Prometheus /metrics on :{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
