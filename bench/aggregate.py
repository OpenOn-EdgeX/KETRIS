#!/usr/bin/env python3
"""벤치 집계 (Exp_50) — results.jsonl + traces → 3계열 지표 표.

지표 정의 (report 에 동일 문구 기록):
  OC 효율      = Σ_i (co-location sps_i / solo sps_i)  — "순차 실행 대비 총 처리량"
                 (순차 = 같은 벽시계에서 각자 1/N 씩 독점 실행 ⇒ 정규화 합 1.0 기준)
  활용률(smi)  = nvidia-smi utilization.gpu 1초 샘플 평균 — "임의 커널이 도는 시간 비율"
                 (SM 몇 %를 쓰든 1개라도 돌면 100 — 공간 점유 아님)
  활용률(cupti-union) = 전 테넌트 커널 구간 유니온 / 벽시계 — smi 정의의 트레이스 판
  활용률(cupti-sum)   = Σ 테넌트 커널 시간 / 벽시계 — 중첩 포함(>1 = 공간 중첩 실증)
  논리 배정률  = Σr (배정 LSU 몫 합 — 입력값)
  최소 유지율  = min_i (sps_i / solo sps_i / r_norm_i ...) → 여기서는 절대 잔존율 min 보고
"""
import argparse
import json
import os
import statistics as st
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))


def kernel_intervals(path):
    iv = []
    try:
        for line in open(path):
            if line.startswith("K|"):
                p = line.split("|")
                s, d = int(p[1]), int(p[2])
                iv.append((s, s + d))
    except OSError:
        pass
    return iv


def union_busy(all_iv):
    if not all_iv:
        return None, None, None
    all_iv.sort()
    t0, t1 = all_iv[0][0], max(e for _, e in all_iv)
    tot = sum(e - s for s, e in all_iv)
    merged = 0
    cs, ce = all_iv[0]
    for s, e in all_iv[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            merged += ce - cs
            cs, ce = s, e
    merged += ce - cs
    wall = t1 - t0
    return merged / wall, tot / wall, wall / 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(D, "results.jsonl"))
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.results)]
    rows = [r for r in rows if "error" not in r]

    # 솔로 기준선: (role, batch) -> mean sps
    solo = defaultdict(list)
    for r in rows:
        if r["tag"].startswith("solo_"):
            (n, m), = r["members"].items()
            if m["sps"]:
                solo[(m["role"], m["batch"])].append(m["sps"])
    sbase = {k: st.mean(v) for k, v in solo.items()}
    print("== 솔로 기준선 ==")
    for k, v in sorted(sbase.items()):
        print(f"  {k}: {v:.4f} sps (n={len(solo[k])})")

    # 조건별 집계
    by_tag = defaultdict(list)
    for r in rows:
        if not r["tag"].startswith("solo_"):
            by_tag[r["tag"]].append(r)

    out = {}
    hdr = (f"{'tag':18s} {'Σr':>5s} {'OC':>12s} {'min잔존':>7s} "
           f"{'util%':>6s} {'cupti-u':>8s} {'cupti-Σ':>8s} {'mem_MB':>7s} "
           f"{'charged 점유':>20s}")
    print("\n" + hdr)
    for tag, reps in sorted(by_tag.items()):
        ocs, minrets, utils, cu_u, cu_s, mems, shares = [], [], [], [], [], [], []
        for r in reps:
            rets = []
            all_iv = []
            ch = {}
            for n, m in r["members"].items():
                if m["sps"] is None:
                    continue
                base = sbase.get((m["role"], m["batch"]))
                if base:
                    rets.append(m["sps"] / base)
                all_iv += kernel_intervals(m["trace"])
                if m.get("charged_us"):
                    ch[n] = m["charged_us"]
            if rets:
                ocs.append(sum(rets))
                minrets.append(min(rets))
            if r.get("util_mean") is not None:
                utils.append(r["util_mean"])
            if r.get("mem_used_max_mb"):
                mems.append(r["mem_used_max_mb"])
            u, s_, _ = union_busy(all_iv)
            if u is not None:
                cu_u.append(u)
                cu_s.append(s_)
            if ch and sum(ch.values()) > 0:
                tot = sum(ch.values())
                shares.append({n: v / tot for n, v in ch.items()})
        if not ocs:
            continue
        m_oc, s_oc = st.mean(ocs), (st.stdev(ocs) if len(ocs) > 1 else 0)
        share_s = ""
        if shares:
            ks = sorted(shares[0])
            share_s = "/".join(f"{st.mean([s[k] for s in shares]):.3f}" for k in ks)
        out[tag] = {"alloc_rate": reps[0]["alloc_rate"], "policy": reps[0]["policy"],
                    "oc": round(m_oc, 4), "oc_std": round(s_oc, 4),
                    "min_retention": round(st.mean(minrets), 4),
                    "util_smi": round(st.mean(utils), 1) if utils else None,
                    "cupti_union": round(st.mean(cu_u), 4) if cu_u else None,
                    "cupti_sum": round(st.mean(cu_s), 4) if cu_s else None,
                    "mem_max_mb": max(mems) if mems else None,
                    "charged_share": share_s, "n": len(ocs)}
        o = out[tag]
        print(f"{tag:18s} {o['alloc_rate']:5.2f} {m_oc:6.3f}±{s_oc:.3f} "
              f"{o['min_retention']:7.3f} {o['util_smi']:6.1f} "
              f"{str(o['cupti_union']):>8s} {str(o['cupti_sum']):>8s} "
              f"{str(o['mem_max_mb']):>7s} {share_s:>20s}")

    if a.json_out:
        json.dump({"solo": {f"{k}": v for k, v in sbase.items()}, "conditions": out},
                  open(a.json_out, "w"), ensure_ascii=False, indent=2)
        print(f"\n{a.json_out} 기록")


if __name__ == "__main__":
    main()
