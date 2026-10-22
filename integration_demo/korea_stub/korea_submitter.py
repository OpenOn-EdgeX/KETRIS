#!/usr/bin/env python3
# 이 컴포넌트는 고려대 기관 구현으로 대체됩니다. 인터페이스(A-2/A-3) 검증용 최소 스텁입니다.
#
# 최소 범위: 제출 전 A-2 /capacity로 총 할당률 조회 → A-3 규약대로 annotation+env 부여해 파드 제출.
# 하지 않는 것: SLO 알고리즘, 우선순위 스케줄링, 실제 라우팅 정책("총 할당률 X 이상이면 회피" 자리표시만).
import json, os, subprocess, sys, urllib.request

CAP_API = os.environ.get("KRAKEN_CAPACITY_API", "http://127.0.0.1:8090/capacity")
NS = os.environ.get("KRAKEN_DEMO_NS", "kraken-demo")
TMPL = os.path.join(os.path.dirname(__file__), "workload_pod.tmpl.yaml")


def get_capacity():
    with urllib.request.urlopen(CAP_API, timeout=5) as r:
        return json.load(r)


def route_decision(cap):
    """자리표시 라우팅: 총 할당률이 간섭 임계 넘으면 경고만(다른 노드 로직은 고려대 몫)."""
    dev = cap["devices"][0]["capacity"]
    hint = cap["devices"][0]["interference_hint"]
    ratio = dev["allocation_ratio"]
    zone = "간섭" if ratio >= hint["interference_above"] else ("전이" if ratio >= hint["no_interference_below"] else "무간섭")
    print(f"[korea-stub] 총 할당률 {ratio:.2f} ({zone}) adv={dev['advertised_lsu']} factor={dev['overcommit_factor']}")
    if not dev["lsu_measured"]:
        print("[korea-stub] ⚠️ 이 장비 LSU 잠정값 — 신뢰도 낮음으로 취급")
    return zone


def submit(name, wclass, batch, lsu, stage=""):
    # A-3: annotation(스펙면) + env(현행 제어경로). 프로덕션은 KETI webhook이 annotation→env 변환.
    tmpl = open(TMPL).read()
    y = (tmpl.replace("@@NAME@@", name).replace("@@NS@@", NS).replace("@@CLASS@@", wclass)
         .replace("@@BATCH@@", str(batch)).replace("@@LSU@@", str(lsu)).replace("@@STAGE@@", stage or wclass))
    subprocess.run(["kubectl", "apply", "-f", "-"], input=y.encode(), check=True,
                   stdout=subprocess.DEVNULL)
    print(f"[korea-stub] 제출: {name} class={wclass} stage={stage} batch={batch} lsu={lsu} (annotation+env)")


if __name__ == "__main__":
    cap = get_capacity()
    route_decision(cap)
    # 데모: 학습(aggressor, compute) + 서빙(victim, memory) 한 쌍
    lsu = int(os.environ.get("KRAKEN_DEMO_LSU", "133"))
    submit("demo-train", "compute", 32, lsu, stage="prefill")
    submit("demo-serve", "memory", 1, lsu, stage="decode")
