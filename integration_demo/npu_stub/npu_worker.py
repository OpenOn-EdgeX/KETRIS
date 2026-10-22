#!/usr/bin/env python3
# NPU 추론 워커 (Exp_83). rngd-verify/phase2 pe_worker.py 계보 — 검증된 방식 재사용.
#   furiosa_llm.LLM(model, devices="npu:1:<lo>-<hi>") 로 PE 블록 바인딩, 반복 추론.
# 모델=furiosa-ai/Qwen2.5-0.5B-Instruct (tp≤4 공식 아티팩트 1종, HF 캐시 hostPath).
import json, os, sys, time

TAG = os.environ.get("WORKER_NAME", "npu")
DUR = float(os.environ.get("DURATION_S", "20"))
SYNC = os.environ.get("SYNC_DIR", "/sync")
MODEL = os.environ.get("NPU_MODEL", "furiosa-ai/Qwen2.5-0.5B-Instruct")
# plugin(Exp_70)이 Allocate시 주입: KETI_NPU_DEVICES=npu:1:0-3, KETI_NPU_PE_RANGE=0-3
VIS = os.environ.get("KETI_NPU_PE_RANGE", os.environ.get("NPU_VISIBLE_PE", ""))
DEVICES = os.environ.get("KETI_NPU_DEVICES", "")   # "npu:1:0-3" 직접 사용 가능


def emit(ev, **kw):
    print(json.dumps({"tag": TAG, "ts": round(time.time(), 3), "event": ev, **kw}), flush=True)


def devices_arg():
    # plugin이 준 KETI_NPU_DEVICES(npu:1:0-3)를 명시 바인딩. 실바인딩은 호스트 furiosa-smi ps로 별도 검증.
    if DEVICES:
        return DEVICES
    if VIS:
        return f"npu:1:{VIS}"
    return None


emit("start", pid=os.getpid(), visible_pe=VIS, model=MODEL)
from furiosa_llm import LLM, SamplingParams  # noqa: E402
emit("imported")

kwargs = {}
dev = devices_arg()
if dev:
    kwargs["devices"] = dev
t0 = time.time()
try:
    llm = LLM(MODEL, **kwargs)
except Exception as e:
    emit("load_failed", error=f"{type(e).__name__}: {e}", elapsed=round(time.time() - t0, 3))
    sys.exit(2)
emit("loaded", elapsed=round(time.time() - t0, 3), devices=dev or "auto")

# 동기 배리어(선택) — go가 있으면 맞추되, 없으면 짧은 유예 후 바로 진행(NPU는 GPU 페어 co-start 불요)
try:
    os.makedirs(SYNC, exist_ok=True)
    open(os.path.join(SYNC, f"ready_{TAG}"), "w").close()
    for _ in range(6):   # 최대 3초만 대기
        if os.path.exists(os.path.join(SYNC, "go")):
            break
        time.sleep(0.5)
except Exception:
    pass
emit("running")

sp = SamplingParams(max_tokens=64, temperature=0.0)
prompt = "What is the capital of France? Answer in one sentence."
n, toks, lat = 0, 0, []
t0 = time.time()
deadline = t0 + DUR
while time.time() < deadline:
    s = time.time()
    out = llm.generate(prompt, sp)
    lat.append(time.time() - s)
    n += 1
    toks += len(out.outputs[0].token_ids) if hasattr(out.outputs[0], "token_ids") else 64
    if n == 1:
        emit("first_output", text=out.outputs[0].text.strip()[:80])
el = time.time() - t0
lat.sort()
rec = {"iters": n, "tok_per_s": round(toks / el, 1), "elapsed": round(el, 2),
       "median_lat_s": round(lat[len(lat) // 2], 4) if lat else None, "visible_pe": VIS}
emit("done", **rec)
try:
    json.dump(rec, open(os.path.join(SYNC, f"result_{TAG}.json"), "w"))
except Exception:
    pass
llm.shutdown()
emit("shutdown")
