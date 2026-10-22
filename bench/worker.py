#!/usr/bin/env python3
"""Exp_17 — co-location 처리량 워커 (Exp_15/16 worker 확장, 프로세스 1개 = 테넌트 1개).

역할 (Exp_13 분류 실측 근거):
  prefill      opt-6.7b fp16, b=BATCH S=1024 전체 forward 반복 — C/M 0.912 COMPUTE
  decode       opt-6.7b fp16, b=BATCH KV 재사용 1-token 반복  — C/M 0.025 MEMORY
  bert_infer   bert-large-uncased fp16, b=32 s=512 forward    — C/M 0.778 COMPUTE (Exp_13 동일 설정)
  rtrain       torchvision resnet50 fp32, b=64 fwd+bwd+SGD    — C/M 0.337 MEMORY (Exp_13 동일 설정)
  phased       측정창 전반 prefill → 후반 decode (Phase 4 국면 전환, PHASE_SPLIT 기본 0.5)

프로토콜: 로드+워밍업 → <sync>/ready_<name> 생성 → <sync>/go 폴링 →
go 이후 DURATION_S 초 측정 → <sync>/result_<name>.json 기록.
timeline: 측정 중 ~0.5s 간격 (elapsed, n) 샘플 — Phase 4 국면 분해용.

env: WORKER_ROLE, WORKER_NAME, SYNC_DIR, DURATION_S, BATCH, PHASE_SPLIT
     (MPS 는 CUDA_MPS_* / CUDA_MPS_ACTIVE_THREAD_PERCENTAGE 로 부모가 주입)
"""
import json, os, time

ROLE = os.environ["WORKER_ROLE"]
NAME = os.environ["WORKER_NAME"]
SYNC = os.environ["SYNC_DIR"]
DUR = float(os.environ.get("DURATION_S", "15"))
# [Exp_142 시험 규약 ㄱ] 작업 길이 지터 — Exp_132 와 같은 정의를 승계한다
#   (워크로드 길이에 0~1s 무작위, 위상 균등화 — turnaround_measure.py:42).
#   JITTER=1 일 때만. 기본 꺼짐 = 기존 동작과 한 비트도 다르지 않다.
#   sps = n/elapsed 로 자체 계산되므로 길이 지터는 처리량 지표를 왜곡하지 않는다.
if os.environ.get("JITTER", "") == "1":
    import random as _rnd
    DUR += _rnd.uniform(0.0, 1.0)
B = int(os.environ.get("BATCH", "1"))
SPLIT = float(os.environ.get("PHASE_SPLIT", "0.5"))
S = 1024

# ── [Exp_89] 런타임 관측 스트리밍 ────────────────────────────────────────────
# 폐루프 제어의 센서. per-step 지연을 롤링 창으로 p95 집계해 SYNC/obs_<NAME>.json 에
# 주기적으로 기록 → 호스트 폐루프 데몬이 파일을 읽는다(libbless feed.log 패턴과 동형,
# 파드→호스트 네트워킹 불요). env KRAKEN_OBS=1 일 때만 동작(다른 용도 무영향).
import threading
from collections import deque
_OBS_ON = os.environ.get("KRAKEN_OBS", "0") == "1"
_OBS_INTERVAL = float(os.environ.get("KRAKEN_OBS_INTERVAL", "0.5"))  # 보고 주기(s)
_OBS_WINDOW = float(os.environ.get("KRAKEN_OBS_WINDOW", "1.0"))      # 롤링 창(s)
_OBS_BUF = deque(maxlen=20000)   # (monotonic_ts, latency_s)
_OBS_LOCK = threading.Lock()


def _obs_push(t_now, lat_s):
    if _OBS_ON:
        with _OBS_LOCK:
            _OBS_BUF.append((t_now, lat_s))


class _ObsList(list):
    """[Exp_115 2부] 지연을 append 하면 관측 버퍼에도 자동으로 흘리는 리스트.

    ★역할별로 `_obs_push()` 를 손으로 붙이지 않는다. Exp_113~114 에서 `_obs_push` 가
      `run_bert` 안 한 곳에만 있어 decode·prefill 에서는 obs 파일이 생기지 않았고,
      폐루프가 개입 판단 자체를 못 했다(transitions=0, 관측 줄 0). 역할이 늘 때마다
      같은 결함이 반복되므로 **기록 지점 자체**를 계측형으로 바꾼다.

    모든 역할이 per-step 지연을 `lat.append(...)` 로 남기는 공통 관용구를 쓰므로,
    `lat = []` 를 `lat = _obs_list()` 로 바꾸면 그 역할은 자동으로 관측을 보고한다.
    새 역할도 같은 관용구를 따르면 추가 작업이 없다.
    """

    def append(self, v):
        list.append(self, v)
        _obs_push(time.monotonic(), v)


def _obs_list():
    return _ObsList() if _OBS_ON else []


def _obs_reporter():
    tenant = os.environ.get("BLESS_TENANT", "")
    cls = os.environ.get("KRAKEN_WORKLOAD_CLASS", "")
    tmp = os.path.join(SYNC, f"obs_{NAME}.json.tmp")
    dst = os.path.join(SYNC, f"obs_{NAME}.json")
    while True:
        time.sleep(_OBS_INTERVAL)
        now = time.monotonic()
        with _OBS_LOCK:
            win = [l for (t, l) in _OBS_BUF if now - t <= _OBS_WINDOW]
        if not win:
            continue
        p = _pct(win)
        sps = round(len(win) / _OBS_WINDOW, 3)
        rec = {"name": NAME, "tenant": tenant, "class": cls, "role": ROLE,
               # [Exp_151 3부] pid = 파드 세대 식별자. devID 는 kubelet 이 재사용할
               #   수 있어(G456 실측) 폐루프가 (tenant, pid) 쌍으로 세대를 가른다.
               "pid": os.getpid(),
               "p50_ms": p.get("p50_ms"), "p95_ms": p.get("p95_ms"),
               "p99_ms": p.get("p99_ms"), "sps_inst": sps, "ts": time.time()}
        try:
            with open(tmp, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, dst)   # 원자적 교체(데몬이 부분쓰기 안 읽게)
        except Exception:
            pass


def _obs_start():
    if _OBS_ON:
        threading.Thread(target=_obs_reporter, daemon=True).start()


def wait_go(k=None):
    name = "go" if k is None else f"go_{k}"
    p = os.path.join(SYNC, name)
    while not os.path.exists(p):
        time.sleep(0.01)


def ready(k=None):
    suffix = "" if k is None else f"_{k}"
    open(os.path.join(SYNC, f"ready_{NAME}{suffix}"), "w").close()


class Pause:
    """[Exp_121 2-A] 측정 도중 한 번 쉬었다가 다시 일하는 구간을 만든다.

    재분배가 켜지면 '쉬는 동안 남는 몫을 다른 쪽이 가져간다'. 되돌아왔을 때
    **자기 몫을 되찾는 데 얼마나 걸리는가**가 SLA 의 핵심이라 이 훅이 필요하다.
    PAUSE_AT_S 가 0 이면 아무 동작도 하지 않는다(기존 역할 그대로).
    """
    def __init__(self):
        self.at = float(os.environ.get("PAUSE_AT_S", "0") or 0)
        self.len = float(os.environ.get("PAUSE_LEN_S", "0") or 0)
        self.done = False
        self.t0 = self.t1 = None

    def maybe(self, el):
        """루프 안에서 매 step 호출. 쉬었으면 True."""
        if self.done or self.at <= 0 or self.len <= 0 or el < self.at:
            return False
        self.done = True
        self.t0 = round(el, 3)
        time.sleep(self.len)
        self.t1 = round(el + self.len, 3)
        return True

    def rec(self):
        return {"pause_at_s": self.t0, "pause_resume_s": self.t1,
                "pause_len_s": self.len if self.done else 0}


class Timeline:
    def __init__(self):
        self.samples = []
        self.next_t = 0.5

    def tick(self, el, n):
        if el >= self.next_t:
            self.samples.append((round(el, 2), n))
            self.next_t = el + 0.5


def load_opt():
    import torch
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(
        "facebook/opt-6.7b", dtype=torch.float16,
        local_files_only=True).to("cuda:0").eval()
    ids = torch.randint(100, 30000, (B, S), device="cuda:0")
    return torch, model, ids


def _pct(xs):
    """[Exp_77] per-step 지연 원시 샘플 → p50/p95/p99 (ms). 꼬리 보존."""
    if not xs:
        return {}
    s = sorted(xs)
    q = lambda p: round(s[min(len(s) - 1, int(p * len(s)))] * 1000, 3)
    return {"p50_ms": q(0.50), "p95_ms": q(0.95), "p99_ms": q(0.99),
            "mean_ms": round(sum(s) / len(s) * 1000, 3), "n_lat": len(s)}


def run_prefill(rec):
    torch, model, ids = load_opt()
    tl = Timeline()
    lat = _obs_list()                               # [Exp_77] per-step(=prefill forward=TTFT proxy) 지연
    with torch.no_grad():
        for _ in range(3):
            model(ids, use_cache=False)
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = 0
        pz = Pause()                        # [Exp_121 2-A]
        while True:
            s0 = time.monotonic()
            model(ids, use_cache=False)
            torch.cuda.synchronize()
            lat.append(time.monotonic() - s0)   # 이 step 의 실지연
            n += 1
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
            if pz.maybe(el):                # [Exp_121 2-A] 쉬는 구간
                tl.tick(time.monotonic() - t0, n)
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               tokens_per_s=round(n * B * S / el, 1), timeline=tl.samples,
               latency=_pct(lat), **pz.rec())          # ★TTFT p50/p95/p99


def run_decode(rec):
    torch, model, ids = load_opt()

    def prefill():
        o = model(ids, use_cache=True)
        return o.logits[:, -1:].argmax(-1), o.past_key_values

    tl = Timeline()
    with torch.no_grad():
        nxt, past = prefill()
        for _ in range(10):
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        torch.cuda.synchronize()
        nxt, past = prefill()
        pos = S
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = n_reprefill = 0
        pz = Pause()                                    # [Exp_121 2-A]
        lat = _obs_list()                               # [Exp_77] 토큰 간 지연
        while True:
            s0 = time.monotonic()
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
            n += 1; pos += 1
            reset = pos >= 2000
            if reset:                          # OPT max_pos 한계 전 리셋
                nxt, past = prefill(); pos = S; n_reprefill += 1
            torch.cuda.synchronize()
            if not reset:                      # reprefill step 은 토큰 지연 아님 — 제외
                lat.append(time.monotonic() - s0)
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
            if pz.maybe(el):        # [Exp_121 2-A] 쉬는 구간
                t_pause = time.monotonic() - t0
                tl.tick(t_pause, n)
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               tokens_per_s=round(n * B / el, 1), n_reprefill=n_reprefill,
               timeline=tl.samples, latency=_pct(lat), **pz.rec())


def run_phased(rec):
    """전반 SPLIT×DUR prefill(use_cache=False) → 후반 decode. b=1 고정 (기준 조합)."""
    torch, model, ids = load_opt()
    tl = Timeline()
    with torch.no_grad():
        for _ in range(3):
            model(ids, use_cache=False)
        # decode 경로 워밍업
        o = model(ids, use_cache=True)
        nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        for _ in range(5):
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        del past, nxt, o
        torch.cuda.synchronize()
        ready(); wait_go()
        t_switch = DUR * SPLIT
        t0 = time.monotonic(); n_pre = 0
        while True:
            model(ids, use_cache=False)
            torch.cuda.synchronize()
            n_pre += 1
            el = time.monotonic() - t0
            tl.tick(el, n_pre)
            if el >= t_switch:
                break
        el_pre = time.monotonic() - t0
        o = model(ids, use_cache=True)                 # 국면 전환: KV 구축
        nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        torch.cuda.synchronize()
        t1 = time.monotonic(); n_dec = 0; pos = S
        while True:
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
            n_dec += 1; pos += 1
            if pos >= 2000:
                o = model(ids, use_cache=True)
                nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
                pos = S
            torch.cuda.synchronize()
            el = time.monotonic() - t0
            tl.tick(el, n_pre + n_dec)
            if el >= DUR:
                break
    el_dec = time.monotonic() - t1
    rec.update(steps=n_pre + n_dec, elapsed=round(el, 3),
               sps=round((n_pre + n_dec) / el, 4),
               prefill_steps=n_pre, prefill_elapsed=round(el_pre, 3),
               prefill_sps=round(n_pre / el_pre, 4),
               decode_steps=n_dec, decode_elapsed=round(el_dec, 3),
               decode_sps=round(n_dec / el_dec, 4),
               t_switch=round(t_switch, 2), timeline=tl.samples)



def _decode_generic(rec, model_name):
    """범용 b=B decode (gpt2 계열 — Exp_49 후보). load_opt 프로토콜 준용."""
    import torch
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(
        model_name, dtype=torch.float16, local_files_only=True).to("cuda:0").eval()
    tl = Timeline()
    with torch.no_grad():
        ids = torch.randint(100, 30000, (B, 64), device="cuda:0")
        o = model(ids, use_cache=True)
        nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        for _ in range(5):
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = 0; pos = 64
        while True:
            o = model(nxt, past_key_values=past, use_cache=True)
            nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
            n += 1; pos += 1
            if pos >= 900:
                ids = torch.randint(100, 30000, (B, 64), device="cuda:0")
                o = model(ids, use_cache=True)
                nxt, past = o.logits[:, -1:].argmax(-1), o.past_key_values
                pos = 64
            torch.cuda.synchronize()
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               timeline=tl.samples)


def run_gpt2m_decode(rec):
    _decode_generic(rec, "gpt2-medium")


def _cnn_infer(rec, ctor):
    import torch, torchvision
    model = getattr(torchvision.models, ctor)(weights="DEFAULT").to("cuda:0", torch.float16).eval()
    x = torch.randn(B, 3, 224, 224, device="cuda:0", dtype=torch.float16)
    tl = Timeline()
    with torch.no_grad():
        for _ in range(5):
            model(x)
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = 0
        while True:
            model(x)
            torch.cuda.synchronize()
            n += 1
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               timeline=tl.samples)


def run_vgg16_infer(rec):
    _cnn_infer(rec, "vgg16")


def run_mobilenet_infer(rec):
    _cnn_infer(rec, "mobilenet_v3_large")



def run_synth(rec):
    """Exp_54 — 멀티스트림/스레드 합성 워크로드.
    env: SYNTH_STREAMS(스레드당 스트림 수), SYNTH_THREADS, SYNTH_SHARED(1=default 공유).
    matmul 8회마다 synchronize (배치 창 청구 지점 = 기존 워크로드와 동형)."""
    import threading
    import torch
    streams = int(os.environ.get("SYNTH_STREAMS", "1"))
    threads = int(os.environ.get("SYNTH_THREADS", "1"))
    shared = os.environ.get("SYNTH_SHARED", "0") == "1"
    dev = "cuda:0"
    a = torch.randn(1024, 1024, device=dev)
    b = torch.randn(1024, 1024, device=dev)
    torch.cuda.synchronize()
    ready(); wait_go()
    t0 = time.monotonic()
    stop_t = t0 + DUR
    counts = [0] * threads
    tl = Timeline()

    def loop(tid):
        ss = ([torch.cuda.default_stream()] if shared
              else [torch.cuda.Stream() for _ in range(streams)])
        i = 0
        while time.monotonic() < stop_t:
            with torch.cuda.stream(ss[i % len(ss)]):
                (a @ b).sum()
            i += 1
            if i % 8 == 0:
                torch.cuda.synchronize()
                counts[tid] = i
        torch.cuda.synchronize()
        counts[tid] = i

    ths = [threading.Thread(target=loop, args=(k,)) for k in range(threads)]
    for th in ths:
        th.start()
    while any(th.is_alive() for th in ths):
        el = time.monotonic() - t0
        tl.tick(el, sum(counts))
        time.sleep(0.5)
    el = time.monotonic() - t0
    rec.update(steps=sum(counts), elapsed=round(el, 3),
               sps=round(sum(counts) / el, 4), timeline=tl.samples,
               synth={"streams": streams, "threads": threads, "shared": shared})



def run_tvmodel(rec):
    """Exp_56 — torchvision 모델 범용 추론 프로브. env: TV_MODEL, (batch=B)."""
    import torch, torchvision
    name = os.environ["TV_MODEL"]
    ctor = getattr(torchvision.models, name)
    try:
        model = ctor(weights="DEFAULT")
    except Exception:
        model = ctor(weights=None)
    model = model.to("cuda:0", torch.float16).eval()
    sz = 299 if name.startswith("inception") else 224
    x = torch.randn(B, 3, sz, sz, device="cuda:0", dtype=torch.float16)
    tl = Timeline()
    with torch.no_grad():
        for _ in range(5):
            model(x)
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = 0
        while True:
            model(x)
            torch.cuda.synchronize()
            n += 1
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               timeline=tl.samples, model=name)


def run_hfmodel(rec):
    """Exp_56 — HF 인코더 추론 프로브. env: HF_MODEL, HF_KIND(text|vision)."""
    import torch
    name = os.environ["HF_MODEL"]
    kind = os.environ.get("HF_KIND", "text")
    if kind == "vision":
        from transformers import AutoModelForImageClassification as AM
        model = AM.from_pretrained(name, dtype=torch.float16,
                                   local_files_only=True).to("cuda:0").eval()
        inp = {"pixel_values": torch.randn(B, 3, 224, 224, device="cuda:0",
                                           dtype=torch.float16)}
    else:
        from transformers import AutoModel as AM
        model = AM.from_pretrained(name, dtype=torch.float16,
                                   local_files_only=True).to("cuda:0").eval()
        inp = {"input_ids": torch.randint(100, 20000, (B, 128), device="cuda:0")}
    tl = Timeline()
    with torch.no_grad():
        for _ in range(5):
            model(**inp)
        torch.cuda.synchronize()
        ready(); wait_go()
        t0 = time.monotonic(); n = 0
        while True:
            model(**inp)
            torch.cuda.synchronize()
            n += 1
            el = time.monotonic() - t0
            tl.tick(el, n)
            if el >= DUR:
                break
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               timeline=tl.samples, model=name)


def run_bert(rec):
    import torch
    from transformers import AutoModel
    model = AutoModel.from_pretrained(
        "bert-large-uncased", dtype=torch.float16,
        local_files_only=True).to("cuda:0").eval()
    bb = B if B > 0 else 32          # [Exp_78] BATCH env 반영 (victim=b1 저지연 서빙)
    ids = torch.randint(100, 30000, (bb, 512), device="cuda:0")
    mask = torch.ones_like(ids)
    reps = int(os.environ.get("REPEATS", "1"))   # [Exp_80] 라운드 반복(모델로드 상각·콜드런 배제)
    rounds = []
    with torch.no_grad():
        for _ in range(5):                        # 워밍업(콜드런 배제) — 로드 직후 1회만
            model(input_ids=ids, attention_mask=mask)
        torch.cuda.synchronize()
        for k in range(reps):
            ready(k if reps > 1 else None); wait_go(k if reps > 1 else None)
            lat = _obs_list()
            t0 = time.monotonic(); n = 0
            while True:
                s0 = time.monotonic()
                model(input_ids=ids, attention_mask=mask)
                torch.cuda.synchronize()
                s1 = time.monotonic()
                lat.append(s1 - s0)
                # [Exp_115] 수동 _obs_push 제거 — lat(_ObsList) 이 자동으로 흘린다.
                #   (중복 push 는 롤링 창 표본을 2배로 부풀려 p95 를 왜곡한다)
                n += 1
                el = s1 - t0
                if el >= DUR:
                    break
            rounds.append({"sps": round(n / el, 4), "elapsed": round(el, 3),
                           "latency": _pct(lat)})
    spss = sorted(r["sps"] for r in rounds)
    med = spss[len(spss) // 2]
    rec.update(batch=bb, reps=reps, rounds=rounds,
               sps=med, sps_min=spss[0], sps_max=spss[-1],
               latency=rounds[len(rounds) // 2]["latency"])


def run_rtrain(rec):
    import torch
    import torchvision
    model = torchvision.models.resnet50(weights=None).to("cuda:0").train()
    opt = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    crit = torch.nn.CrossEntropyLoss()
    x = torch.randn(64, 3, 224, 224, device="cuda:0")
    y = torch.randint(0, 1000, (64,), device="cuda:0")
    tl = Timeline()

    def step():
        opt.zero_grad(set_to_none=True)
        loss = crit(model(x), y)
        loss.backward()
        opt.step()

    for _ in range(5):
        step()
    torch.cuda.synchronize()
    ready(); wait_go()
    t0 = time.monotonic(); n = 0
    while True:
        step()
        torch.cuda.synchronize()
        n += 1
        el = time.monotonic() - t0
        tl.tick(el, n)
        if el >= DUR:
            break
    rec.update(steps=n, elapsed=round(el, 3), sps=round(n / el, 4),
               tokens_per_s=None, timeline=tl.samples)


def main():
    rec = {"name": NAME, "role": ROLE, "batch": B, "pid": os.getpid(),
           "duration_req": DUR}
    fn = {"prefill": run_prefill, "decode": run_decode, "phased": run_phased,
          "bert_infer": run_bert, "rtrain": run_rtrain,
           "gpt2m_decode": run_gpt2m_decode, "vgg16_infer": run_vgg16_infer,
           "mobilenet_infer": run_mobilenet_infer,
           "synth": run_synth, "tvmodel": run_tvmodel,
           "hfmodel": run_hfmodel}.get(ROLE)
    if fn is None:
        raise SystemExit(f"unknown role {ROLE}")
    _obs_start()                               # [Exp_89] 관측 리포터(KRAKEN_OBS=1 시)
    fn(rec)
    with open(os.path.join(SYNC, f"result_{NAME}.json"), "w") as f:
        json.dump(rec, f)


if __name__ == "__main__":
    main()
