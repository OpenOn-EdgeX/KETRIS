# KRAKEN 시간 분할 의미론 — 정본 (Exp_30 작업 2)

**정본 지위**: 시간 분할(time_credit/charged_us/게이트/envelope) 시맨틱의 단일 정본.
흩어져 있던 서술(각 Exp report 내 §)을 이 문서가 대체하며, 상충 시 이 문서가 우선한다.
모든 서술은 실측 근거 각주를 갖는다 — 근거 없는 서술은 등재하지 않는다 (Exp_30 출력 규약).

관련 정본: REST 표면은 `docs/api/controller_api.yaml`, 상태머신은 같은 파일의
`x-state-machine` (machine.py LEGAL 전이표 기준). LSU 환산·노출은
`docs/semantics/lsu.md`, NPU 요청 게이트(시간 축 단독)는
`docs/semantics/npu_gate.md`.

---

## §1. charged_us 정의 — 배치 벽시계 창 점유, GPU busy 아님

`charged_us` 는 **테넌트 배치(step)의 벽시계 창 점유 시간**이다. 창 내부의 idle 을
포함하며, CUPTI 가 측정하는 GPU busy 밀도가 아니다.[^1]

두 지표가 갈라지는 이유와 수치 사례:

| 페어 | 내부 charged 점유(A) | 외부 CUPTI GPU 시간 | 갭 |
|---|---|---|---|
| BERT×BERT (동종) | 0.6669 | 0.6679 | **0.001** |
| opt prefill×decode (이종) | 0.6654 | 0.7614 | **0.096** |

동종 페어에서는 두 정의가 일치한다(갭 0.001) — BERT-Large 학습의 GPU 시간 커버리지가
12.4%에 불과해도 게이트 제어·과금은 외부 진실과 일치한다.[^2] 이종 페어의 갭 0.096은
사각지대가 아니라 **정의 차이**다: prefill 은 창당 busy 밀도가 높고 decode 는 낮아,
창 점유(charged)와 busy 시간(CUPTI)이 체계적으로 갈라진다.[^3]

**운영 함의**: 점유 보고·SLA 지표로 charged 를 쓸 때는 "창 점유" 정의를 명기해야 한다
(이종 혼합에서 GPU busy 기준 보고와 최대 ~0.1 어긋날 수 있음). 외부 기관 인터페이스에는
`docs/api/kw_interface_draft.md` 가 이 정의를 참조 링크로 명기한다.

## §2. 게이트 우선순위 — priority × time 직렬 AND

priority 게이트와 time 게이트는 **직렬 AND** 다. 런치는 두 게이트를 모두 통과해야
실행되며, urgent 는 priority 축의 특권일 뿐 **time 축의 특권이 아니다**.[^4]

libbless 게이트 순서: `priority_queue_gate` → reconf/pause → 선점 재확인 →
`time_credit_gate`. time 게이트는 urgent 를 검사하지 않는다.[^5]

실측 규칙표 (단독 테넌트 U, 4s 창, 무개입 기준 1411 steps/s):[^6]

| 조건 | U 처리율 | charged_us | 해석 |
|---|---|---|---|
| URGENT × credit 소진 | **0.0** | 0 | urgent 는 time 게이트를 우회하지 못함 |
| URGENT × credit 보유(duty 0.5) | 540.2 | 2,010,873 (duty 0.5027) | urgent 실행도 과금됨 |
| URGENT × 무제한 | 1459.2 | 4,027,464 (duty 1.007) | 차감만 생략, 계정은 지속 |
| NORMAL × urgent_pending | **0.0** | 0 | 억제 게이트 (E2E 선점 메커니즘) |

**운영 규칙 R-1**: urgent SLA 정책은 **time_credit 동반 전환**을 함께 규정해야 한다 —
테넌트를 urgent 로 올릴 때 controller 가 해당 테넌트를 release(무제한) 또는 충분한
비율로 함께 전환하지 않으면 time 게이트가 여전히 막아 "urgent=즉시 실행"이 성립하지
않는다.[^7]

urgent 는 **상태**다(1회성 아님): `urgent_request` 는 `urgent_clear` 까지 유지되며,
플래그가 프로세스-로컬이므로 교차 테넌트 선점은 controller 팬아웃
(urgent(X) = X 에 `priority_set 0` + 나머지 전원에 `urgent_request`)으로 성립한다.[^8]

## §3. 커버리지 제약 — 가시 런치 개재 조건

**"가시 런치가 개재되지 않는 순수 라이브러리-런치 루프(예: raw cuBLAS 벤치)는
게이트·과금 대상 외다."**[^9]

- 제어가 깨지는 조건은 커버리지가 낮을 때가 아니라 **가시 런치 개재가 0일 때**다
  (순수 GEMM 루프 — O-1 사례, matmul 가시율 0.1%). step 마다 aten 런치가 개재되는
  실워크로드(BERT·opt·resnet)는 커버리지 12.4%에서도 제어가 유효하다(갭 0.001).[^10]
- 메커니즘: 게이트는 가시 런치에서 CPU 스레드를 블록하고, 같은 스레드의 후속 cuBLAS
  런치도 함께 정지한다(스레드 직렬화로 제어 전파). 과금은 배치 벽시계 창 기준이므로
  우회 커널의 시간도 간접 포함된다.[^11]
- O-1 (원인): cuBLAS 는 `cuGetProcAddress` 로 런처를 획득해 libbless 심볼 인터포지션을
  우회한다. dlsym/cuGetProcAddress 훅은 gating_lib.so 에만 있고 libbless.so 에는 없다.[^12]
- **검증 범위 (Exp_54 확장)**: 제어 전파는 단일 런처 스레드에 더해 **멀티 스트림
  (2/4)·멀티 스레드(2/4)·공유 스트림에서도 실증**되었다 — 6조건 ×3회에서 게이트
  오차 ≤0.0004 로 단일 스트림(0.0002)과 동급. 스레드별 배치 창은 thread_local 이나
  credit 은 전역 atomic 이라 회계가 유지된다. ★단 **MPS 하에서는 비-default 스트림
  워크로드에 O-6 제약이 남는다**(공간 노브 ↔ 멀티스트림 배타).[^13]

## §4. envelope 의미론 — Σ=1.0 기본, in-envelope 분배, 완화는 옵션

- 기본은 **in-envelope Σ=1.0**: tick(10ms)마다 피더가 분배하는 예산의 불변식은
  `Σbudget ≤ TICK×1e6×env_scale`, 기본 `env_scale=1.0` (Exp_16 격리 조건).[^14]
- 분배 규칙: 분모는 `max(1, Σratio)` — **Σ≤1 이면 ratio 를 절대 duty 로 해석**
  (단독 게이트 duty 0.5 = Exp_20/22 gate 케이스), **Σ>1 이면 비례 정규화**.[^15]
- **★ratio 는 분수(duty) 계약이다** (Exp_77 명시): 등록 ratio 는 `요청LSU/물리LSU`
  같은 **분수**여야 한다. raw LSU 값(예: 89)을 넣으면 단일 armed 테넌트에서 denom=Σ 가
  되어 duty=1.0(무스로틀) — 하한 1.0 의 의도(Σ≤1 절대 duty)가 깨진다. Exp_74 auto-arm 이
  raw LSU 를 넣어 relaxed aggressor 가 full duty 로 새는 계약 위반이 있었고, Exp_77 에서
  `ratio=요청LSU/물리LSU` 로 **복원**(device-plugin bless_feeder `ratioOf`). 수동
  `/feeder/ratios` 도 분수를 넣는다. **다음에 raw 값을 넣지 말 것.**
- **완화(예: 1.6)는 옵션이며 근거가 있다**: in-envelope(Σ=1.0)는 비율은 정밀하지만
  co-location 초과 이득(OC~1.2)을 포기해 절대 효율을 깎는다(OC 0.40~0.56).
  Σ=1.6 완화만으로 0.764 까지 회복 — envelope 스케일은 **"격리 강도 vs 활용률"
  조정 노브**다.[^16]
- 폐루프 재현 정밀도: 비율 재설정 후 점유 A 0.4998 / B 0.5002 (오차 0.0002).[^17]

## §5. 축 비대칭 — space=스폰 시점, time=런타임

- **공간(SM) 축은 런타임 재설정 불가** — 4경로 실증: (1) ctx 파괴/재생성 →
  cuBLAS 크래시, (2) MPS `set_active_thread_percentage` → 기동 중 클라이언트 무효,
  (3) ctx 풀 + 포인터 스왑 → torch matmul 미반영, (4) cuGetProcAddress/dlsym 인터포즈
  → 캡처 0 (cudart 12.9).[^18] 근본 원인: torch/cuBLAS 커널은 init 시점에 고정된
  컨텍스트에서 실행되며, 앱 협조 없는 컨텍스트 이주는 불가.[^19]
- 따라서 **공간 재배치 = 스폰 시점 결정**(BLESS_LIMIT_PCT), **런타임 축 = time
  (time_credit)뿐**이다.[^19] time 축은 "누가 갖는가"를 바꾸지만 "총량"은 늘리지
  못한다(게이트 하 OC 1.137→1.059).[^20]
- 공간 축을 런타임에 바꾸려면 **make-before-break 재스폰 경로**를 쓴다:
  게이트 on → 새 BLESS_LIMIT_PCT 로 standby 스폰(새 ctx) → ready 확인 → 구 테넌트
  드레인 → 게이트 해제. 콜드 재스폰 비용 16.33±0.15s(13.4GB 모델), 핫 standby
  핸드오프 갭 ≈0s, 손익분기 T*≈57s.[^21] controller 제품화 경로는
  `POST /lifecycle/swap` (핸드오프 겹침 0.111s 재현).[^22]
- 시간(time_credit) × 공간(SM 비율)은 **직교 합성 가능**: 50/50 SM + credit 2:1 →
  런치 점유 0.667 (목표 0.667).[^23]

## §6. BLESS_LIMIT_PCT — 암묵 50% 캡, 명시 설정 원칙, full=99

- **암묵 50% 캡**: `libbless.cpp:468` 기본값 `pct=50`, getenv 는 `0<v<100` 일 때만
  반영 — **미설정이면 SM 50% 캡**이고, **v=100 도 제외되어 50 으로 떨어진다**.
  전체 SM 을 쓰려면 **99** 를 설정한다 (full=99 관행의 근거).[^24]
- **명시 설정 원칙**: libbless 부착 워크로드는 BLESS_LIMIT_PCT 를 항상 명시한다.
  controller swap 경로는 이를 강제한다(`env["BLESS_LIMIT_PCT"]` 명시 주입,
  loop_api.py:187).[^25]
- **BLESS_LIMIT_PCT 가 MPS env 를 무효화한다**: libbless 의 명시 affinity ctx
  (`CU_EXEC_AFFINITY_TYPE_SM_COUNT`)가 채택되면 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 는
  무효 (99 + MPS 30% 실측 38.4/s = 캡 미작동). libbless 부착 워크로드의 공간 분할
  유일 노브는 BLESS_LIMIT_PCT 이고, MPS env 는 비-libbless 프로세스용이다.[^26]
  ★이상은 **space_mode=ctx(기본) 한정** — mps_env 모드에서는 역전된다(§6-2).

## §6-1. 커널 수 credit 경로 — 비활성·Fix2/3 영구 기각 (Exp_55)

libbless 는 두 credit 모드를 갖는다: `credit_mode=1` **TIME**(time_credit,
현행 유일 실사용 경로)과 `credit_mode=0` **KERNEL**(커널 수 기반). 제어 평면
(controller feeder·device-plugin·bench)은 KERNEL 모드 명령(`credit_set`/
`credit_refill`)을 **한 번도 호출하지 않는다** — 실사용 경로는 전부 TIME 이다.[^33]

따라서 Exp_7 이 제안했던 **Fix2**(청크 `cur>>2` — `tl_burst` 누설 억제)와
**Fix3**(`credit_refill` 이월+cap — `credit_set` 덮어쓰기의 잔량 손실 보정)는
**영구 기각**한다. 근거 3: ① 대상 경로가 실사용에서 도달 불가 ② Fix3 가 겨냥한
잔량 손실은 시간 경로에 구조적으로 없다(`time_add`=누적, §4 + Exp_16 보강 ②로
적자도 이월) ③ 시간 경로 정확도가 이미 목표 달성 — 2:1/1:2/3:1/**극단 10:1**
에서 오차 **0.0002~0.0008**(15s 창 ×3회).[^33] KERNEL 경로를 되살릴 일이 생기면
그때 재평가한다(코드 주석에 결정 기록).

## §6-2. BLESS_SPACE_MODE — 공간 제한 위임 방식 (Exp_61, O-6b/O-7 해소 경로)

- **`BLESS_SPACE_MODE=ctx|mps_env`** (기본 `ctx` — 하위호환, 기존 동작 무변경.
  미지 값은 무음 수용 금지: 경고 후 ctx). 네이밍은 "affinity 비활성"(부정형)이
  아니라 **"공간 제한을 누구에게 위임하는가"**(ctx=exec-affinity 제한 컨텍스트 /
  mps_env=MPS 서버)를 명명한다.[^35]
- **mps_env 모드**: libbless 는 제한 ctx 를 만들지 않고 launch 시 ctx 전환도 없다.
  공간 제한은 **스폰 시점에 주입된 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 를 MPS 가
  직접 해석** — libbless 는 그 값을 소비하지 않는다(초기화 로그에 관측용 echo 만).
  ctx 전환이 사라지므로 **O-6b(별도 스레드 launch 크래시)·O-7(훈련 hang)이 원천
  해소**된다.[^35]
- **관측 가능성**: 초기화 로그 `sm_limit` 3상태 — `applied`(ctx 실적용) /
  `FALLBACK-unlimited (O-3)`(ctx 모드인데 MPS 미접속) / `delegated-mps-env`
  (mps_env 위임). 판정은 항상 이 로그로 한다(Exp_60 측정 함정). `sm_stats` 는
  space_mode 를 함께 보고하며, mps_env 의 ctx 부재는 O-3 폴백 카운터에
  집계하지 않는다(의도된 경로).
- **런타임 재설정**: mps_env 도 불가(프로세스 단위 env) — ctx 모드와 **동급**이며
  make-before-break 재스폰(§5)으로 처리한다. `reconf_sm`/`set_limit_pct` 소켓
  명령은 mps_env 에서 **명시 거부**된다(무음 no-op 금지).
- **규약 변경 범위(전환 시)**: ① device-plugin Allocate 주입 env 키
  `BLESS_LIMIT_PCT` → `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`(+`BLESS_SPACE_MODE=mps_env`)
  ② controller 의 s 전달(swap/spawn env) 동일 교체 ③ mps_env + BLESS_LIMIT_PCT
  병용은 경고(공간 제한에 미사용). 232 반영은 Exp_62(다운타임 창)에서.
- **운용 권고**: 훈련 워크로드·멀티스레드 서빙은 mps_env(O-7/O-6b), 그 외는 ctx
  유지 가능. ctx 실적용 시 초기화 로그에 O-7 위험 고지 1줄이 출력된다(훈련 감지는
  launch 파라미터에 이름/형상이 없어 불가 — Exp_13/48 — 라 무조건 고지로 갈음).

## §7. 봉투 완화 규약 — 정책 3종, 가드 4종 (Exp_40)

이종(COMPUTE×MEMORY) 페어에서 in-envelope(Σ=1.0)의 절대 효율 손실을 회복하기 위한
**opt-in 정책 규약**이다. 기본값은 언제나 `strict` — 완화는 명시 선택으로만 진입한다.[^27]

| 정책 | 수혜자(COMPUTE) | 피해자(MEMORY) | 의미 |
|---|---|---|---|
| `strict` (기본) | s·t 계약 몫대로 게이트 | 동일 | 현행 in-envelope 의미론 (§4) |
| `relaxed_hetero` | s=1.0(99), t=r **게이트** | s=1.0(99), **미무장** | 완화 = 봉투 완화 (시간 축) |
| `capped_hetero` | relaxed 와 동일 | relaxed + **s=r 공간 상한** | 완화 + 공간 상한 (계약 명시용) |

**★정확한 표현 — 흐리지 말 것**: relaxed 에서 피해자는 "t=1.0 예산을 부여받는" 것이
**아니다**. 피해자는 **미무장(armed=False, 게이트 자체가 없음)** 이며, 따라서
`budgets()` 의 **Σ 정규화 대상에서 제외**된다. armed 집합에만 in-envelope 불변식이
걸리므로 수혜자의 t=r 예산은 그대로 절대 duty 로 해석된다(§4 분배 규칙과 정합).[^28]
"피해자 t=1.0 등록"은 feeder 장부상의 ratio 기록일 뿐 게이트 동작이 아니다.

**봉투 방어는 거부가 아니라 정규화다**: strict 에서 Σratio>1 이 되는 등록은
거부되지 않고 이력에 경고만 남으며, `budgets()` 의 `denom=max(1,Σ)` 비례 정규화가
봉투를 방어한다(하위호환 — test_10/test_14 가 감시).[^29]

**가드** (전부 fallback_reason 기록 — 무음 폴백 금지):[^30]

| # | 조건 | 처치 |
|---|---|---|
| 1 | 이종 혼합 아님 (C≥1+M≥1 불충족 — 동종/MIXED/UNCERTAIN 포함/단독) | strict 폴백 |
| 2 | 분류 confidence ≠ HIGH | strict 폴백 (Exp_37 오분류 회피) |
| 3 | capped + 3-tenant 이상 | **relaxed 폴백 + 경고** (3+ 실측은 relaxed 만 — Exp_58) |
| 4 | capped + MPS 부재 | **relaxed 폴백 + 경고** (O-3: s 상한이 무음 no-op 되는 것 방지) |

★**N-tenant 확장 (Exp_58 §3)**: relaxed_hetero 는 2-tenant 한정(Exp_40 v1)에서
**N-tenant 이종 혼합**으로 확장됐다. 공격자/피해자 판정은 클래스 기반
멤버별(모든 COMPUTE = t=r 게이트, 모든 MEMORY = 게이트 해제)이며, 3자 실측
2조합에서 성립: **C2+M1**(prefill+bert+decode, 0.33×3) OC 0.503→**1.021**
(+103%, 피해자 decode 잔존율 0.211→0.766, charged 는 두 공격자만 0.53/0.47 —
다중 공격자 게이트 동시 동작), **C1+M2**(prefill+decode+densenet121) OC
0.540→**1.325**(+145%, 두 피해자 동시 해방 0.206→0.645 / 0.192→0.564).
strict 3T 는 Exp_50 (0.506) 재현 0.503 으로 브리지 확인. capped 는 3+ 미실측
— 가드 3 폴백.[^33]

**근거 (Exp_39b 효과 분해)**: +47% 이득의 본질은 봉투 완화다 — 완화 효과
C−U = **+0.367**, 공간 상한 순효과 P−C = **−0.030**(캡 비용). 따라서 권고는
"봉투 정책 + 계약 시 s 상한"이며, capped 는 성능이 아니라 **계약 명시**를 위한
옵션이다.[^31] 무격리(F, 양쪽 미무장)는 decode 를 굶겨 기각됐다.[^32]

**★capped 는 space_mode=mps_env(§6-2) 하에서도 성립 — 실측 완료 (Exp_61)**:
공간 상한을 MPS env 로 걸어 5팔을 재측정한 결과 C−U +0.3665 / **P−C −0.0324** —
Exp_39b(ctx 방식)와 부호·크기 동일. **capped 의 비용은 상한의 구현 방식과
무관**하며, 테넌트별 상이 pct(프로세스별 env 주입)도 구조상 문제없음이 확인됐다.
"MPS 정책 결정 선행" 유보는 해제 — 남은 결정은 232 운용(MPS 도입 여부)뿐.[^36]

---

[^1]: Exp_29 §3 (reports/Exp_29_gate_coverage/report.md:70-74).
[^2]: Exp_29 §0·§3 (report.md:11-13, 62); BERT-Large 학습 GPU 시간 커버리지 0.124 (§2, report.md:42).
[^3]: Exp_29 §3 (report.md:63, 71-74).
[^4]: Exp_27 §5 (reports/Exp_27_urgent_wiring/report.md:114-117).
[^5]: Exp_27 §2 (report.md:55-56); libbless.cpp:876-896.
[^6]: Exp_27 §5 규칙표 (report.md:107-112).
[^7]: Exp_27 §5·§6 (report.md:116-117, 131).
[^8]: Exp_27 §2 (report.md:51, 58-61); libbless.cpp:710-720.
[^9]: Exp_29 §5 iii — R-4 채택 문구 (report.md:94).
[^10]: Exp_29 §3 (report.md:76-77); O-1 은 Exp_27 §5 (report.md:119-127).
[^11]: Exp_29 §3 (report.md:68-70).
[^12]: Exp_27 §5 (report.md:122-127); Exp_13 LD_PRELOAD 가시율 65% 맥락 동일.
[^13]: Exp_29 §3 (report.md:68-69) — 실증은 단일 런처 스레드 하네스였다. ★**Exp_54 에서 해소**: 멀티 스트림(2/4)·멀티 스레드(2/4)·공유 스트림 6조건 ×3회 실측에서 게이트 오차 ≤0.0004(단일 스트림 0.0002와 동급) — "게이트는 스트림/스레드 무관" 확정 (reports/Exp_54_stream_coverage/report.md §Phase 1). 단 그 과정에서 O-6(비-default 스트림 워크로드가 libbless 컨텍스트 전환 때문에 실행 불가) 발견·수정했고, **MPS 하에서는 O-6 제약이 잔존**(공간 노브 ↔ 멀티스트림 배타 — 같은 report §한계).
[^14]: Exp_16 (reports/Exp_16_time_credit_230/report.md:38); feeder.py:11,22,102-111 (DEFAULT_ENV_SCALE=1.0).
[^15]: Exp_26 §1 (reports/Exp_26_closed_loop_wiring/report.md:49-52); feeder.py:100-113.
[^16]: Exp_16 §Overcommit 효율 (report.md:85-91) — Σ=1.0 OC 0.40~0.56, Σ=1.6 → 0.764.
[^17]: Exp_26 §4-2 (report.md:124-126).
[^18]: Exp_20 §0·§1 (reports/Exp_20_adaptive_loop_230/report.md:19-22, 34-39).
[^19]: Exp_20 §1 (report.md:41-45).
[^20]: Exp_20 §0·§3 (report.md:26-27, 84).
[^21]: Exp_21 §1·§2 (reports/Exp_21_generation_swap_230/report.md:31-52, 53-73).
[^22]: Exp_26 §4-3 (report.md:141-143).
[^23]: Exp_9 §3 (reports/Exp_9_mps_spatial_230/report.md:77-84).
[^24]: Exp_23 §2.3 (reports/Exp_23_coverage_audit/report.md:57).
[^25]: Exp_25 인수인계 → Exp_26 제품화 (controller/loop_api.py:187 "항상 명시 (암묵 50% 캡 회피)").
[^26]: Exp_21 §0·§1·§3 (report.md:16-29, 91-92) — Exp_20 "MPS 50/50" 프레임 정정 포함; Exp_23 §2.3 (report.md:56) 경로 확인.
[^27]: Exp_40 §결론 (reports/Exp_40_envelope_policy/report.md:96-99) — 기본값 strict, opt-in; controller/ratio/pair.py:47 (decide_pair 기본 policy="strict").
[^28]: Exp_40 §Phase 2 (report.md:61-65) — "피해자 t=1.0 은 등록만 하고 미무장"; feeder.py budgets() armed 집합 한정 (§4 [^15] 동일 근거).
[^29]: Exp_40 §Phase 0 (report.md — "기존 feeder=정규화(거부 아님) 확정"); feeder.py register() strict 경고 경로 + budgets() denom=max(1,Σ).
[^30]: Exp_40 §설계 (report.md:33-35); controller/ratio/pair.py fallback_reason 경로. 가드 4(capped+MPS 부재→relaxed 폴백)는 O-3 (Exp_39 — BLESS_LIMIT_PCT 는 MPS 하에서만 실효) 방어.
[^31]: Exp_39b 효과 분해 (reports/Exp_39b_envelope_vs_spatial/report.md — C−U +0.367 / P−C −0.030); 엔진 경유 재현 오차 ≤0.002 (Exp_40 §Phase 3, report.md:99).
[^32]: Exp_39b F 팔 (무격리) — decode 기아로 기각 (report.md §결론).
[^33]: Exp_58 §3 (reports/Exp_58_residuals/report.md) — tri/trim 각 3정책 ×3회 (230 GPU1, MPS 33%×3). 같은 실험에서 관찰된 **O-7**(훈련 워크로드 진행 불가)은 **Exp_60 에서 규명**됐다 — 아래 [^34].
[^34]: **O-7 규명 (Exp_60 §1, reports/Exp_60_o7_cmwindow/report.md — figures/fig1_o7_layers.png)**: 원인은 MPS 도, 훈련 프레임워크도 아니라 **libbless 의 제한 컨텍스트(ctx affinity) 적용**이다. 판정 축은 조건 라벨이 아니라 초기화 로그의 `sm_limit` 값이며(`applied` = ctx 생성됨 / `FALLBACK-unlimited` = 미생성), 43런 층 분리 결과가 완전히 갈린다: **훈련 × ctx 적용 0/16 정상, 훈련 × ctx 생략 12/12 정상(7.344 sps), 추론은 ctx 무관 9/9 정상**. `BLESS_LIMIT_PCT` 는 발현 시점만 바꾼다(99/90/75 = 웜업 실패, 50/33 = 웜업 통과 후 측정 구간). 이는 Exp_17 §8-2 가 "cudnn 워밍업과 SM-affinity ctx 라우팅 상호작용 의심"으로 남긴 **미검 항목의 확인**이자 O-6/O-6b 와 **같은 뿌리**(ctx 전환)다. ★따라서 O-6b 의 (B) 전환안(affinity 대신 MPS env 에 공간 제한 위임)이 O-7 도 해소한다 — 실측 12/12 가 그 경로다(전환 근거 강화). 현 정본 유지 시 제약: **훈련 워크로드는 ctx affinity 없이 운용**(`BLESS_LIMIT_PCT` 미지정 또는 (B) 전환). ★측정 함정 3종도 같은 보고서에 기록: 타임아웃 프로세스가 MPS 서버를 오염시켜 다음 조건이 `cudaErrorDevicesUnavailable` 로 즉사 / 데몬 종료 직후 비-MPS 접근 실패 / `pgrep` 이 타 실험 데몬을 오인해 파이프 미기동(→ 조건이 조용히 FALLBACK 으로 바뀜). Exp_58 의 O-7 층 분리는 이 함정들을 탄 것으로 보이며, 그래서 "무-MPS 는 정상" 같은 대조가 실제로는 ctx 미적용 상태였다.
[^33]: Exp_55 §Phase 1 (reports/Exp_55_debt_closure/report.md — 호출 주체 grep 0건, 비율 4종 실측표); libbless.cpp `kernel_credit_gate` 주석의 결정 기록.
[^35]: **BLESS_SPACE_MODE 정식화 (Exp_61 Phase 1, reports/Exp_61_space_mode_capped_catalog/report.md)**: Exp_57 PoC(`BLESS_NO_CTX_AFFINITY`, 정본 미반영)를 정식 스위치로 승격. 검증 5종 각 3회(230 GPU1, 전용 파이프 /tmp/exp61-mps): ① `sm_limit=delegated-mps-env` 관측성 + 미지값 경고 + mps_env×BLESS_LIMIT_PCT 병용 경고 + reconf 명시 거부 ② 공간 실효 MPS pct 25/50/99 → 280/547/1116 ips(비 0.251/0.490/1.0 비례) ③ **rtrain 3/3 완주**(O-7 해소, sps 5.836±0.002) ④ 멀티스레드 5조건 ×3 전부 OK(O-6b 해소) ⑤ 시분할 무손상 — strict 2:1 charged 0.6671~0.6678(목표 0.667). md5 계보: cpp d6151489→2b4d721f, so 0accdf01→311360d1(CUDA 12.9 빌드). PoC env `BLESS_NO_CTX_AFFINITY` 는 정본에 채택하지 않음(정식 채널 1개 원칙 — 정본에 들어간 적 없어 하위호환 부담 없음).
[^36]: **capped × mps_env 실측 (Exp_61 Phase 2, reports/Exp_61_space_mode_capped_catalog/report.md §2 — figures/fig1_arms.png)**: Exp_39b 프로토콜 그대로(prefill×decode 0.5/0.5, 25s ×3회, 전용 파이프), 공간 상한만 BLESS_LIMIT_PCT → CUDA_MPS_ACTIVE_THREAD_PERCENTAGE 로 교체. U 0.7395 / C 1.106 / P 1.0736 / R 0.8645 / F 1.1922. 페어 30 멤버 전수 `sm_limit=delegated-mps-env` 확인. 배정 규칙 P−R +0.2091(MEMORY 우선 우위 유지), F 는 decode 0.326 굶김으로 동일 기각.
