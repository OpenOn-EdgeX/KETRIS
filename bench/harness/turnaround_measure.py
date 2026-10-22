#!/usr/bin/env python3
"""[Exp_95 Part A → Exp_132 재사용 하네스] 자원 해지 경로 구간 분해 측정.

[Exp_107 T-4] 세 Exp(95/96/97) 이상이 참조하므로 재사용 하네스로 승격했다.
  reports/Exp_95_turnaround/assets/turnaround_measure.py 는 그때의 기록으로 보존한다.
  Exp_132 에서 고친 것: KRAKEN_ 개명 반영, socks 경로 정정(무동작이었음), 회차별 저장.


★끝점 정의: "해지 완료" = **자원이 다시 배정 가능해진 시점**(대기 워크로드가 실제로 배정된 순간).
  Pod 삭제 시점이 아니다(지시서 A-1).

구간과 시각 소스 — K8s 타임스탬프는 **초 해상도**라 ms 분해에 못 쓴다.
그래서 watch 스트림을 로컬에서 수신한 시각을 찍는다(API 서버가 localhost 라 전송 지연 무시 가능).

  T0 워크로드 종료 조건 도달 : runner 가 result_*.json 을 쓴 시각(st_mtime_ns).
                               worker.py 가 마지막 스텝 뒤 마지막으로 하는 일이라 CUDA 해지 이전이다.
  T1 프로세스 exit           : 호스트에서 /proc/<pid> 소멸 관측(5ms 폴링).
                               T0→T1 = CUDA 컨텍스트 해지 + 메모리 회수(1차년도 732ms 구간).
  TC containerd 종료 인지    : `ctr -n k8s.io events` 의 /tasks/exit 수신 시각(런타임이 아는 시점).
                               ★T1→TC = 런타임 감지 지연, TC→T2 = **kubelet 이 런타임보다 늦는 몫**.
                               이 격차가 선제 감지(C-1)로 앞당길 수 있는 상한이다.
  T2 kubelet 종료 인지       : watch — runner 의 containerStatuses[].state.terminated 최초 관측.
                               T1→T2 = PLEG 감지 + 상태 기록(1차년도 719ms 구간).
  T3 Pod 상태 갱신           : watch — runner phase=Succeeded 최초 관측.
  T4 ★자원 갱신(배정 가능)   : watch — 미리 Pending 으로 대기시킨 waiter 에 spec.nodeName 이 붙는 순간.
  T5 (참고) waiter Running   : 컨테이너 기동까지. 지표에는 안 넣는다(모델 로드 제외 원칙과 같은 이유).

측정 구성: runner(전량 LSU 요청, 짧게 돌고 종료) → waiter(같은 LSU 요청, 먼저 제출돼 Pending 대기).
runner 가 끝나야만 waiter 가 배정되므로, waiter 배정 시각이 곧 "자원이 실제로 반환된 시각"이다.

env: NS(prism-turn), LSU(178), DUR(8), BATCH(16), RUNS(3), OUT(/root/exp95_turnaround.json)
"""
import json, os, subprocess, sys, threading, time

NS = os.environ.get("NS", "prism-turn")
LSU = os.environ.get("LSU", "178")
DUR = os.environ.get("DUR", "8")
BATCH = os.environ.get("BATCH", "16")
EXIT_MODE = os.environ.get("EXIT_MODE", "normal")
SC = os.environ.get("SC", "")
FLUSH = os.environ.get("FLUSH", "1")   # [Exp_144] 3부 대조용 — 0이면 sitecustomize flush 생략
# [Exp_153 3부] FT=0 이면 파드 env 로 BLESS_FAST_TEARDOWN=0 을 덮어 끈다.
#   라이브가 기본 켜짐(Exp_144)이라 base(꺼짐) 조건은 스위치 롤아웃 없이
#   파드 env 덮기로 만든다 — 파드 spec env 가 plugin 주입을 이긴다(Exp_134 실측).
#   롤아웃 없이 블록 교대(T-8)가 성립하는 유일한 방법이다.
FT = os.environ.get("FT", "")
# [Exp_97] ★T1 이 PLEG 1초 폴링과 부분 동기화되면 T0→T1 단축이 T1→T2 증가로 상쇄된다.
#   JITTER=1 이면 워크로드 길이에 0~1초 무작위를 더해 T1 위상을 균등화한다.
JITTER = os.environ.get("JITTER", "")   # [Exp_96] "1" 이면 플랫폼이 sitecustomize 주입   # normal | fast(os._exit, CUDA 해지 생략)
RUNS = int(os.environ.get("RUNS", "3"))
OUT = os.environ.get("OUT", "/root/exp95_turnaround.json")
SD = "/root/prism-sync-exp95"
REPO = "/root/workspace/pooling/prism"


def kc(args, **kw):
    return subprocess.run(["kubectl"] + args, capture_output=True, text=True, timeout=30, **kw)


def apply(y):
    return subprocess.run(["kubectl", "apply", "-f", "-"], input=y, text=True,
                          capture_output=True, timeout=30)


RUNNER = """apiVersion: v1
kind: Pod
metadata: {{name: tr-runner, namespace: {ns}}}
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 0
  containers:
    - name: t
      image: prism-infer:opt
      imagePullPolicy: IfNotPresent
      env:
        - {{name: KRAKEN_WORKLOAD_CLASS, value: "memory"}}
        - {{name: WORKER_ROLE, value: "bert_infer"}}
        - {{name: WORKER_NAME, value: "run"}}
        - {{name: SYNC_DIR, value: /sync}}
        - {{name: DURATION_S, value: "{dur}"}}
        - {{name: BATCH, value: "{b}"}}
        - {{name: REPEATS, value: "1"}}
        - {{name: HF_HUB_OFFLINE, value: "1"}}
        - {{name: HF_HOME, value: /hf}}{ftenv}
      command:
        - bash
        - -c
        - |
          {cmd}
      volumeMounts:
        - {{name: bench, mountPath: /bench, readOnly: true}}
        - {{name: hf, mountPath: /hf, readOnly: true}}
        - {{name: sync, mountPath: /sync}}
      resources: {{limits: {{cpu: "5", memory: 16Gi, keti.re.kr/lsu: "{lsu}"}}}}
  volumes:
    - {{name: bench, hostPath: {{path: {repo}/bench, type: Directory}}}}
    - {{name: hf, hostPath: {{path: /root/.cache/huggingface, type: Directory}}}}
    - {{name: sync, hostPath: {{path: {sd}, type: DirectoryOrCreate}}}}
"""

WAITER = """apiVersion: v1
kind: Pod
metadata: {{name: tr-waiter, namespace: {ns}}}
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 0
  containers:
    - name: t
      image: alpine
      imagePullPolicy: IfNotPresent
      command: ["sh","-c","sleep 20"]
      resources: {{limits: {{cpu: "1", memory: 512Mi, keti.re.kr/lsu: "{lsu}"}}}}
"""


# 종료 경로 A/B — worker.py 는 건드리지 않는다(다른 Exp 가 공유).
#   normal: 정상 종료(파이썬 finalize → CUDA 컨텍스트 해지 → exit)
#   fast  : 결과 기록 직후 os._exit(0) — 파이썬 finalize·CUDA 해지를 건너뛴다
CMD = ("python /bench/worker.py" if EXIT_MODE == "normal" else
       "python -c \"import runpy,os; runpy.run_path('/bench/worker.py', run_name='__main__'); os._exit(0)\"")


class CtrEvents(threading.Thread):
    """containerd /tasks/exit 수신 시각 — 런타임이 종료를 아는 시점(TC)."""

    def __init__(self, pid_box):
        super().__init__(daemon=True)
        self.tc = None
        self.stop = False
        self.proc = None
        self.pid_box = pid_box          # runner 의 호스트 PID(늦게 채워짐)

    def run(self):
        self.proc = subprocess.Popen(["ctr", "-n", "k8s.io", "events"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        for line in self.proc.stdout:
            if self.stop:
                break
            if "/tasks/exit" not in line:
                continue
            t = time.time()
            if self.tc is None:
                # runner 컨테이너의 exit 인지 pid 로 확인(다른 파드 종료와 섞이지 않게)
                pid = self.pid_box.get("pid")
                if pid and f'"pid":{pid}' not in line.replace(" ", ""):
                    continue
                self.tc = t


class Watcher(threading.Thread):
    """pods watch 스트림을 받아 관심 전이의 **로컬 수신 시각**을 기록."""

    def __init__(self):
        super().__init__(daemon=True)
        self.marks = {}
        self.stop = False
        self.proc = None

    def mark(self, k, t=None):
        if k not in self.marks:
            self.marks[k] = t if t is not None else time.time()

    def run(self):
        self.proc = subprocess.Popen(
            ["kubectl", "get", "--raw",
             f"/api/v1/namespaces/{NS}/pods?watch=1&timeoutSeconds=600"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        for line in self.proc.stdout:
            if self.stop:
                break
            t = time.time()
            try:
                ev = json.loads(line)
            except Exception:
                continue
            o = ev.get("object", {})
            if o.get("kind") != "Pod":
                continue
            name = o.get("metadata", {}).get("name", "")
            st = o.get("status", {})
            if name == "tr-runner":
                for cs in st.get("containerStatuses", []) or []:
                    if "terminated" in (cs.get("state") or {}):
                        self.mark("T2", t)
                if st.get("phase") in ("Succeeded", "Failed"):
                    self.mark("T3", t)
            elif name == "tr-waiter":
                if o.get("spec", {}).get("nodeName"):
                    self.mark("T4", t)
                if st.get("phase") == "Running":
                    self.mark("T5", t)


def find_pid(timeout=90):
    """runner 컨테이너의 호스트 PID (컨테이너 프로세스는 호스트에서 보인다)."""
    end = time.time() + timeout
    while time.time() < end:
        r = subprocess.run(["pgrep", "-f", "/bench/worker.py"],
                           capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p.isdigit()]
        # bash 래퍼와 python 둘 다 잡힐 수 있다 → python 프로세스를 고른다
        for p in pids:
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    cl = f.read().decode(errors="ignore")
                if "python" in cl:
                    return int(p)
            except Exception:
                pass
        time.sleep(0.05)
    return None


def poll_exit(pid, stop_evt):
    """5ms 폴링으로 프로세스 소멸(=CUDA 컨텍스트 해지 완료 포함) 시각을 잡는다."""
    path = f"/proc/{pid}"
    while not stop_evt.is_set():
        if not os.path.exists(path):
            return time.time()
        time.sleep(0.005)
    return None


def cleanup():
    kc(["delete", "pod", "--all", "-n", NS, "--force", "--grace-period=0"])
    # [Exp_132] 경로 정정 — /var/lib/prism → /var/lib/kraken (Exp_107 개명).
    #   개명 뒤 이 rm 은 **없는 경로를 지우는 조용한 무동작**이었다(T-5).
    #   Exp_95~97 당시엔 실동작했으므로, 고치는 쪽이 그때 조건의 재현이다.
    subprocess.run("rm -rf /var/lib/kraken/socks/keti-gpu0-lsu*", shell=True)
    # [Exp_97 Part D] 임시 우회 제거 — watcher 재조정 루프가 근본 처방으로 들어갔다.
    #   여기서 우회를 빼는 것 자체가 처방의 검증이다(막히면 측정이 끊겨 바로 드러난다).
    time.sleep(3)


import random as _rnd


def one_run(i):
    subprocess.run(f"rm -rf {SD}", shell=True)
    os.makedirs(SD, exist_ok=True)
    cleanup()

    pid_box = {}
    ce = CtrEvents(pid_box)
    ce.start()
    w = Watcher()
    w.start()
    time.sleep(1)

    dur_i = DUR
    if JITTER == "1":
        dur_i = str(round(float(DUR) + _rnd.random(), 3))
    r = apply(RUNNER.format(ns=NS, dur=dur_i, b=BATCH, lsu=LSU, repo=REPO, sd=SD, cmd=CMD,
                     ftenv=(("\n        - {name: BLESS_FAST_TEARDOWN, value: \"0\"}") if FT == "0" else ("\n        - {name: BLESS_FAST_TEARDOWN, value: \"1\"}"
                             "\n        - {name: PYTHONPATH, value: /bench}"
                             "\n        - {name: BLESS_FT_FLUSH, value: \"%s\"}" % FLUSH)
                            if SC == "1" else "")))
    if r.returncode != 0:
        print("runner apply 실패:", r.stderr.strip()[:200]); return None

    pid = find_pid()
    if not pid:
        print("runner PID 못 찾음"); return None
    pid_box["pid"] = pid

    # ready 대기 후 waiter 제출 → Pending 확정시킨 뒤 go
    for _ in range(300):
        if os.path.exists(f"{SD}/ready_run"):
            break
        time.sleep(0.1)
    r = apply(WAITER.format(ns=NS, lsu=LSU))
    if r.returncode != 0:
        print("waiter apply 실패:", r.stderr.strip()[:200]); return None
    # waiter 가 Pending 으로 자리잡을 시간
    time.sleep(3)
    ph = kc(["get", "pod", "tr-waiter", "-n", NS, "-o", "jsonpath={.status.phase}"]).stdout
    if ph != "Pending":
        print(f"  ⚠ waiter 가 Pending 이 아님({ph}) — 자원 경합 전제가 깨짐")
        return None

    stop_evt = threading.Event()
    res = {}
    th = threading.Thread(target=lambda: res.update(t1=poll_exit(pid, stop_evt)), daemon=True)
    th.start()

    open(f"{SD}/go", "w").close()

    # runner 결과 파일 = T0
    t0 = None
    for _ in range(600):
        try:
            t0 = os.stat(f"{SD}/result_run.json").st_mtime
            break
        except FileNotFoundError:
            time.sleep(0.05)
    th.join(timeout=120)
    stop_evt.set()

    # T4 까지 대기
    end = time.time() + 60
    while time.time() < end and "T4" not in w.marks:
        time.sleep(0.05)
    time.sleep(2)
    w.stop = True
    ce.stop = True
    for p_ in (w.proc, ce.proc):
        if p_:
            p_.terminate()

    m = dict(w.marks)
    m["TC"] = ce.tc
    m["T0"] = t0
    m["T1"] = res.get("t1")
    sps = None
    try:
        sps = json.load(open(f"{SD}/result_run.json"))["sps"]
    except Exception:
        pass
    m["sps"] = sps
    cleanup()
    return m


def seg(m, a, b):
    if m.get(a) and m.get(b):
        return round((m[b] - m[a]) * 1000, 1)
    return None


def main():
    kc(["create", "ns", NS])
    runs = []
    for i in range(RUNS):
        print(f"── run {i+1}/{RUNS}", flush=True)
        m = one_run(i)
        if not m:
            print("   실패 — 건너뜀"); continue
        row = {
            "run": i + 1,
            "T0_T1_컨텍스트해지": seg(m, "T0", "T1"),
            "T1_TC_런타임인지": seg(m, "T1", "TC"),
            "TC_T2_kubelet지연": seg(m, "TC", "T2"),
            "T1_T2_kubelet인지": seg(m, "T1", "T2"),
            "T2_T3_Pod상태갱신": seg(m, "T2", "T3"),
            "T3_T4_자원갱신_배정": seg(m, "T3", "T4"),
            "합계_T0_T4": seg(m, "T0", "T4"),
            "참고_T4_T5_컨테이너기동": seg(m, "T4", "T5"),
            "sps": m.get("sps"),
        }
        runs.append(row)
        print("   " + json.dumps(row, ensure_ascii=False), flush=True)
        # [Exp_132] 회차마다 저장 — n=100 세션이 중단돼도 모은 표본을 잃지 않는다.
        json.dump(runs, open(OUT, "w"), ensure_ascii=False, indent=1)
    json.dump(runs, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"\n저장: {OUT}  (성공 {len(runs)}/{RUNS})")


if __name__ == "__main__":
    main()
