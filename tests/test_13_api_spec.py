#!/usr/bin/env python3
"""Test 13: API 명세-코드 정합 (Exp_30) — GPU 불필요, 정적 대조.

docs/api/controller_api.yaml (정본 명세)의 경로·메서드 목록과
코드 라우팅 표면(kraken_controller.py 직접 라우트 + loop_api.py 위임 라우트,
디스패치 prefix 포함)을 자동 대조한다.
  · 명세에만 있는 라우트 / 코드에만 있는 라우트 → FAIL
  · loop_api 라우트가 디스패치 prefix 로 도달 불가 → FAIL
  · x-state-machine ↔ machine.py LEGAL 전이표 불일치 → FAIL
이후 라우트 추가 시 명세 갱신을 강제하는 회귀 장치 (Exp_31+ 대비).
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

import yaml

ROOT = os.path.join(os.path.dirname(__file__), '..')
SPEC_PATH = os.path.join(ROOT, 'docs', 'api', 'controller_api.yaml')
CTRL_PATH = os.path.join(ROOT, 'controller', 'kraken_controller.py')
LOOP_PATH = os.path.join(ROOT, 'controller', 'loop_api.py')

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, cond, info=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


# ── 코드 라우팅 표면 추출 (정적 파싱) ─────────────────────────────────────────
def _func_body(src, name):
    """def <name> 부터 다음 (톱레벨 또는 메서드) def 직전까지."""
    m = re.search(rf"def {re.escape(name)}\b", src)
    if not m:
        raise AssertionError(f"함수 없음: {name}")
    start = m.start()
    nxt = re.compile(r"\n(?:    )?def ").search(src, m.end())
    return src[start: nxt.start() if nxt else len(src)]


def _exact_paths(body):
    """`path == "..."` / `self.path == "..."` 등식 비교 라우트."""
    return set(re.findall(r'path\s*==\s*"(/[^"]*)"', body))


def _prefixes(body):
    """`path.startswith((...))` 디스패치 prefix 튜플."""
    out = set()
    for tup in re.findall(r'startswith\(\((.*?)\)\)', body, re.DOTALL):
        out |= set(re.findall(r'"(/[^"]*)"', tup))
    return out


ctrl_src = open(CTRL_PATH).read()
loop_src = open(LOOP_PATH).read()

code = {"get": set(), "post": set()}
prefixes = {"get": set(), "post": set()}

body_get = _func_body(ctrl_src, "do_GET")
body_post = _func_body(ctrl_src, "do_POST")
code["get"] |= _exact_paths(body_get)
code["post"] |= _exact_paths(body_post)
prefixes["get"] = _prefixes(body_get)
prefixes["post"] = _prefixes(body_post)

loop_get = _exact_paths(_func_body(loop_src, "handle_get"))
loop_post = _exact_paths(_func_body(loop_src, "handle_post"))
code["get"] |= loop_get
code["post"] |= loop_post

# ── 명세 로드 ────────────────────────────────────────────────────────────────
spec_doc = yaml.safe_load(open(SPEC_PATH))
spec = {"get": set(), "post": set()}
for p, item in spec_doc.get("paths", {}).items():
    for method in item:
        if method in ("get", "post"):
            spec[method].add(p)

# ── 1. 경로·메서드 전수 대조 ─────────────────────────────────────────────────
print("== 1. 명세 ↔ 코드 라우팅 표면 전수 대조 ==")
for method in ("get", "post"):
    only_spec = sorted(spec[method] - code[method])
    only_code = sorted(code[method] - spec[method])
    check(f"{method.upper()}: 명세에만 있는 라우트 없음", not only_spec,
          f"명세 전용: {only_spec}" if only_spec else f"{len(spec[method])}종")
    check(f"{method.upper()}: 코드에만 있는 라우트 없음", not only_code,
          f"코드 전용: {only_code}" if only_code else "")

total = len(code["get"]) + len(code["post"])
check("전체 라우트 수 = 명세 라우트 수", total ==
      len(spec["get"]) + len(spec["post"]), f"코드 {total}종")

# ── 2. 위임 라우트의 디스패치 prefix 도달성 ──────────────────────────────────
print("== 2. loop_api 위임 라우트 → 디스패치 prefix 도달성 ==")
for method, routes in (("get", loop_get), ("post", loop_post)):
    unreachable = sorted(r for r in routes
                         if not any(r.startswith(pf) for pf in prefixes[method]))
    check(f"{method.upper()}: 위임 라우트 전부 prefix 도달 가능", not unreachable,
          f"도달 불가: {unreachable}" if unreachable
          else f"prefix {sorted(prefixes[method])}")

# 직접 라우트가 디스패치 prefix 에 가려지지 않는지 (dispatch 가 exact 보다 선행)
print("== 3. 직접 라우트 ↔ prefix 충돌 없음 ==")
for method in ("get", "post"):
    direct = (code[method] - loop_get - loop_post)
    shadowed = sorted(r for r in direct
                      if any(r.startswith(pf) for pf in prefixes[method]))
    check(f"{method.upper()}: 직접 라우트가 prefix 에 가려지지 않음", not shadowed,
          f"가려짐: {shadowed}" if shadowed else "")

# ── 4. 상태 사전 ↔ machine.py LEGAL 전이표 ──────────────────────────────────
print("== 4. x-state-machine ↔ machine.LEGAL ==")
from lifecycle.machine import LEGAL  # noqa: E402

xsm = spec_doc.get("x-state-machine", {})
spec_states = set(xsm.get("states", {}))
code_states = set(LEGAL) | {s for v in LEGAL.values() for s in v}
check("상태 집합 일치", spec_states == code_states,
      f"명세 {sorted(spec_states)} vs 코드 {sorted(code_states)}")
spec_trans = {k: set(v) for k, v in xsm.get("legal_transitions", {}).items()}
code_trans = {k: set(v) for k, v in LEGAL.items()}
check("합법 전이표 일치", spec_trans == code_trans,
      "" if spec_trans == code_trans else f"명세 {spec_trans} vs 코드 {code_trans}")

# ── 요약 ─────────────────────────────────────────────────────────────────────
n_pass = sum(results)
print(f"\n결과: {n_pass}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
