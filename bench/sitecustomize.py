"""[Exp_96] 플랫폼 주입 종료 경로 — 워크로드 무수정.

파이썬은 시작 시 sys.path 의 sitecustomize 를 자동 import 한다. 플랫폼이 PYTHONPATH 로
이 파일을 얹으면 워크로드 코드를 고치지 않고 종료 경로를 바꿀 수 있다.

왜 파이썬 레벨인가 (Exp_96 §3 실측):
  CUDA 컨텍스트 해지(660ms)는 **인터프리터 종료 과정 안**에서 일어난다. C 레벨에서는
  끼어들 수 없다 — exit() 가로채기 불가(glibc 내부 호출), libbless on_exit 는 너무 늦고
  (746.7→607.6ms), Py_Finalize 는 심볼 미노출로 후킹 불가. 유일한 진입점이 파이썬 atexit 다.

★종료 코드 보존이 이 설계의 핵심 위험이다. os._exit 는 인터프리터가 계산한 종료 코드를
  버리므로, 실패한 워크로드가 성공(0)으로 보일 수 있다. 아래 세 경로를 모두 잡는다:
    1) 미처리 예외      → sys.excepthook
    2) sys.exit(n)      → sys.exit 래핑
    3) raise SystemExit → 위 둘로 안 잡히면 종료 코드를 신뢰할 수 없으므로 **빠른 종료를 포기**하고
                          정상 종료 경로로 되돌린다(느리지만 정확). 안전 우선.

BLESS_FAST_TEARDOWN=1 일 때만 동작. 미설정 시 아무 것도 하지 않는다.
"""
import os

if os.environ.get("BLESS_FAST_TEARDOWN") == "1":
    import atexit
    import builtins
    import sys

    _st = {"code": 0, "known": True}     # known=False 면 빠른 종료를 쓰지 않는다

    # ── 1) 미처리 예외 ────────────────────────────────────────────────
    _orig_hook = sys.excepthook

    def _hook(t, v, tb):
        _st["code"] = 1
        _orig_hook(t, v, tb)

    sys.excepthook = _hook

    # ── 2) sys.exit(n) ───────────────────────────────────────────────
    _orig_exit = sys.exit

    def _exit(code=None):
        if code is None or code is True:
            _st["code"] = 0 if code is None else 1
        elif isinstance(code, int):
            _st["code"] = code
        else:
            _st["code"] = 1              # 문자열 등 → 파이썬은 1 로 종료하며 메시지 출력
        _orig_exit(code)

    sys.exit = _exit

    # ── 3) raise SystemExit(n) 직접 — sys.exit 래퍼를 안 거치므로 따로 잡는다 ──
    #
    # [Exp_134] ★기존 구현은 동작하지 않았다. `SystemExit.__init__ = ...` 는
    #   내장 타입이라 TypeError 로 실패하고, 그 실패를 받는 폴백이 없어서
    #   `_st["known"]` 이 True 로 남았다 → **실패한 작업이 종료 코드 0(Succeeded)로
    #   보고됐다** (Exp_96 §5-3 이 잔여 결함으로 적어둔 것. Exp_134 2-C 라이브 실측:
    #   `raise SystemExit(4)` → phase=Succeeded, exitCode=0).
    #
    #   `raise SystemExit(...)` 는 **builtins 의 이름 조회**를 거치므로 그 자리에서
    #   잡을 수 있다. 하위 클래스이므로 `except SystemExit` 도, 인터프리터의 종료
    #   코드 처리(PyErr_GivenExceptionMatches)도 그대로 동작한다.
    #   `sys.exit(n)` 은 C 레벨에서 **진짜** SystemExit 를 던지므로 여기 안 걸린다
    #   (그 경로는 위 ②가 이미 코드를 보존한다).
    #   ⚠️ `type(e) is SystemExit` 로 정확한 타입을 보는 코드는 어긋난다 —
    #      드문 패턴이고, 오보고를 남기는 쪽이 더 나쁘다고 판단했다.
    _RealSystemExit = builtins.SystemExit

    class _WrappedSystemExit(_RealSystemExit):
        def __init__(self, *a):
            _st["known"] = False      # 코드를 신뢰할 수 없다 → 빠른 종료 포기
            _RealSystemExit.__init__(self, *a)

    try:
        builtins.SystemExit = _WrappedSystemExit
    except Exception:
        # 치환조차 못 하면 **안전 측으로 고정** — 빠른 종료를 아예 쓰지 않는다.
        _st["known"] = False

    # ── [Exp_144 1부] 열린 파일 레지스트리 — os._exit 유실 방어 ──────────
    # 왜: os._exit 는 Py_Finalize(열린 io flush·close)를 건너뛴다. 기존 구현은
    #   sys.stdout/stderr 만 flush 해 **워크로드 파일 버퍼가 통째로 유실**됐다
    #   (Exp_134 §7-1 ✕위험, Exp_139 기전 확정, Exp_144 재현: 4090B → 0B).
    #
    # 방식 선택 (1-A 비교 — 둘 다 실측):
    #   ① gc.get_objects() 순회 — 합성 벤치(1M dict)에선 1.9ms 였으나
    #     ★실파드(bert-large torch)에서 **204ms** — 객체 수 선형이라 대형 모델
    #     프로세스에서 이득(T0→T1 ~530ms)의 40%를 먹었다(중간 집계 n=30:
    #     감소율 5.2%로 미달). 실측으로 기각.
    #   ② open 래핑 레지스트리(채택) — builtins.open 을 감싸 열리는 파일을
    #     WeakSet 에 담는다. 비용 O(열린 파일 수). builtins.open 은 io.open 과
    #     같은 객체라 io.open 호출자·pathlib.Path.open·os.fdopen(내부에서
    #     io.open 를 이름 조회) 전부 이 래퍼를 지난다.
    #   공통 한계(기록): C 확장이 자체 FILE*/fd 로 여는 것은 어느 방식으로도
    #   못 잡는다 — §9 잔여 위험.
    # flush 만 한다 — close 는 순서 의존·비용 문제로 범위 밖(지시서 1-B).
    # 상한: 두지 않는다 — 레지스트리 비용은 열린 파일 수 비례(실측 ms 미만)라
    #   발동 근거가 없고, 상한 발동=유실 재발이다. 시간·건수는 항상 로그.
    import io as _io
    import weakref as _weakref

    _open_files = _weakref.WeakSet()
    _real_open = builtins.open

    def _tracking_open(*a, **k):
        f = _real_open(*a, **k)
        try:
            _open_files.add(f)
        except TypeError:                 # weakref 불가 객체 — 추적만 포기
            pass
        return f

    try:
        builtins.open = _tracking_open
        _io.open = _tracking_open         # io.open 직접 호출자(os.fdopen 포함)
    except Exception:
        pass                              # 치환 실패 시 flush 대상이 비어 로그로 드러난다

    def _flush_open_files():
        import time
        t0 = time.monotonic()
        n_ok = n_fail = 0
        for o in list(_open_files):
            try:
                if not o.closed and o.writable():
                    o.flush()
                    n_ok += 1
            except Exception:
                n_fail += 1
        dt_ms = (time.monotonic() - t0) * 1000.0
        try:
            sys.stderr.write("[fast-teardown] 파일 flush %d건 (실패 %d건, %.1fms, "
                             "추적 %d)\n" % (n_ok, n_fail, dt_ms, len(_open_files)))
        except Exception:
            pass

    def _fast_teardown():
        if not _st["known"]:
            return                        # 종료 코드 불확실 → 정상(느린) 경로로 넘긴다
        if os.environ.get("BLESS_FT_FLUSH", "1") != "0":   # [Exp_144] 측정 대조용 opt-out
            _flush_open_files()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(_st["code"])

    atexit.register(_fast_teardown)

    # [Exp_134] 적재 사실을 stderr 에 남긴다 — **이것이 없으면 무동작이다.**
    #   실측(Exp_134 §2-A): 파드 spec 의 PYTHONPATH 가 device-plugin 주입을
    #   이긴다. 즉 워크로드가 PYTHONPATH 를 쓰고 있으면 BLESS_FAST_TEARDOWN=1 은
    #   걸려도 이 파일이 sys.path 에 없어 **조용히 무동작**이 된다. 플러그인은
    #   그것을 알 수 없으므로, 적재된 쪽이 스스로 흔적을 남기는 것이 유일한 방법이다
    #   (파드 로그 = K8s 로그 API 로 호스트에서 읽힌다 — Exp_126 §4-B).
    try:
        sys.stderr.write("[fast-teardown] sitecustomize 적재됨 "
                         "(BLESS_FAST_TEARDOWN=1)\n")
        sys.stderr.flush()
    except Exception:
        pass
