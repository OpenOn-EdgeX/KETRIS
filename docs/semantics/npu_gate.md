# KRAKEN NPU 요청 게이트 의미론 — 정본 (Exp_44/45 → Exp_46 등재, Exp_69 세대 개정)

**정본 지위**: NPU 시분할·분할 의미론의 단일 정본. §0 이 현세대(RNGD) 정본이고,
§1~§7 은 **구세대(Warboy) 기준 실측 기록**으로 보존한다(하드웨어가 232 에 현존).
구세대 구현 정본은 `runtime/npu_proxy/`, 현세대 설계는 `docs/design/npu_gate_v2.md`.

관련 정본: 시간 축 의미론(공유)은 `docs/semantics/time_sharing.md`,
LSU 환산은 `docs/semantics/lsu.md`, REST 표면은 `docs/api/controller_api.yaml`
(`/feeder/register` 의 `resource: npu`).

---

## §0. 현세대(RNGD) 개정 — Exp_67/68 실측 반영 (Exp_69 등재)

구세대 전제 4건이 RNGD(8PE, npu1) 실측으로 다음과 같이 갱신된다.
근거: `reports/Exp_67_rngd_survey/`, `reports/Exp_68_rngd_partition/`,
외부 검증 인수 `reports/rngd_verify_20260803/` (4차수, 2026-08-03~04).

| 구세대 전제 (§1) | 현세대 판정 |
|---|---|
| NPU 분해축 = 시간 단독, 공간 미적용 | **공간 분할 성립** — PE 서브셋(fusion 1/2/4/8 정렬 블록) + PE당 5.94GiB 메모리 하드 파티션. 단 **입도 선택형 정적 분할**이다: 연속 비율 노브(GPU SM 비율류)가 아니라 MIG 에 가까운 고정 슬라이스 선택. 분할 실측: 4PE×2 에서 TTFT p95 간섭 −1.0%, 합계 처리량 통합 대비 ×1.71 |
| PE 배타성이 공간 분할을 원천 차단 | 배타성(EBUSY 즉시 거부, 큐잉 없음)은 그대로다. 그러나 8PE 입도에서는 배타성이 **분할의 집행 메커니즘**으로 작동한다. Warboy 에서 "차단"으로 보였던 것은 2PE 라 나눌 여지가 없었기 때문 |
| 프록시 = NPU 시분할의 유일 경로 | 테넌트 간 조정 계층이 우리뿐인 것은 불변(벤더 스택에 크로스 테넌트·우선순위·선점·몫 없음). 단 형태가 **커널/추론 1건 게이트 → 요청 레벨 게이트**로 바뀐다(furiosa-llm serve 앞단). 공간 축은 프록시 없이 SDK native(`--devices npu:1:0-3` 등) |
| 스케줄링 API 부재 | **정밀화**: 크로스 테넌트 축은 부재 확정. 프로세스 내부 요청 배치 스케줄러는 존재 — furiosa-llm Eager scheduler(버킷 패킹·continuous batching·DP 라우팅, `--scheduler-kind`/`--npu-queue-limit` 노출). 우리 게이트와 중복 아님 |

**★NPU 다중 테넌트의 두 경로** (현세대 운용 원칙):
- **같은 모델** → 공유 serve 1개 + **앞단 요청 게이트**(시간 축 몫·우선순위)
- **다른 모델** → **PE 그룹 분리**(공간 축, 정적) — 그룹당 serve 1개
- 같은 PE 그룹을 두 테넌트가 **시간 교대하는 것은 실용 불가**: 프로세스 교대가
  필요하고 모델 로드가 6.2s(캐시 히트)라 요청 입도 대비 수천 배 비용.

**SR-IOV = 불가 확정 (재시도 금지)**: 펌웨어 1.11.0 이 capability 만 광고하고
VF 를 준비하지 못해, VF 생성(`sriov_numvfs` 쓰기) 즉시 호스트 PCI 서브시스템
교착·카드 버스 이탈(2026-08-04 실사고, 재부팅 외 복구 불가). CLAUDE.md 최상단
경고와 `docs/RESTORE.md` §E 참조. 벤더 펌웨어 수정 전까지 공간 분할의 HW 가상화
축은 없다.

**이종 모델 동거 제약 (벤더 생태계 의존 — 우리가 못 푸는 제약)**: 자체 빌드
아티팩트는 tp=8 전용(`fxb build -tp<8` 은 무경고로 실행 불가 아티팩트 생성)이고
공식 tp≤4 아티팩트는 46종 중 1종(Qwen2.5-0.5B)뿐이라, PE 그룹을 나눠도 **서로
다른 모델을 올릴 수 없다**(2026-08 시점). 해소 조건 = 벤더의 tp≤4 아티팩트 확충
또는 fxb 빌드 수정.

**[Exp_67 리포트 정정]** Exp_67 §1-3 의 "RNGD = LLM 전용(vision 빈 스텁)" 서술은
**과했다**: pip 모델 라이브러리(furiosa-models 2026.2)에 비전 모델이 없는 것이고,
공식 fxb 아티팩트에는 Qwen3-VL 6종(2B/4B/32B × Instruct/Thinking)이 있어 소형은
1장 실행 가능하다. ResNet50 류 CNN 이 furiosa-torch 컴파일 경로에서 불가한 실측은
유효하다. (Exp_67 리포트 원문은 수정하지 않고 여기와 Exp_69 리포트에 정정 기록.)

---

> 이하 §1~§7 은 **구세대(Warboy, npu0) 기준** — 실측 기록으로 보존. RNGD 에
> 이식할 때는 §0 과 `docs/design/npu_gate_v2.md` 를 우선한다.

## §1. 전제 — PE 는 배타 단위, 시간 축 단독

- **PE 내 시분할은 런타임 수준에서 불가**: 동일 PE 이중 runner 는 0.0s 즉시 거부
  ("All NPU device is busy"), furiosa runtime 에 priority/schedule/preempt API 전무,
  네이티브 런타임이라 LD_PRELOAD 인터셉트 지점도 없다.[^1]
- 따라서 NPU 분할 = **공간(PE 단위 배타) + 요청 레벨 시간 축(본 게이트)** 이다.
  ★GPU 의 s(SM 비율) 개념은 NPU 에 **미적용** — PE 는 쪼개지지 않으며, `decide_pair`
  등 공간 분해 경로는 NPU 테넌트에 관여하지 않는다.[^2]
- 유일한 시분할 경로 = **프록시 대행**: npu-proxy 가 PE 의 유일 runner 를 소유하고
  테넌트 요청을 대행 실행하며 run() 호출(추론 1건 ≈1.4ms)을 배분한다.[^3]

## §2. 구조 대응 — 프록시는 NPU 의 libbless

| | GPU | NPU |
|---|---|---|
| data plane | libbless (LD_PRELOAD 주입) | npu-proxy (독립 프로세스, PE 소유) |
| 제어 채널 | `bless-<pid>.sock` | `<sock-dir>/<tenant>.sock` — **명령 계열 동일** |
| control plane | controller feeder (tick 10ms) | **동일 feeder 무변경 재사용**[^4] |
| 회계 | charged_us (`total= kernels=`) | 동일 형식·동일 파서 (`read_time_stats`) |
| 개입 입도 | 커널 런치 (us급) | 추론 1건 (~1.4ms) — 실행 중 비선점 |
| 투명성 | zero-modification | ★앱이 프록시 데이터 프로토콜 경유 필요 |

제어 명령 계약(libbless 와 동일): `time_mode <0|1>` / `time_credit <us|-1>` /
`time_add <us>` / `time_stats`(로그에 `total=<charged_us> kernels=<n>` 발화).[^4]

## §3. 스케줄링 규칙 (확정)

- **선택 = min(charged/granted) 정규화 deficit** (stride) — granted 는 누적 주입
  예산이므로 feeder 가 어떤 비율로 주입하든 비례가 성립한다.
- **strict**: armed 테넌트는 credit≤0 이면 실행하지 않는다(작업보존 폴백 없음) —
  libbless time_credit 과 동일 의미론. 테넌트 부재 시에도 남은 테넌트가 자기 duty 를
  초과하지 않는다(in-envelope 격리, E2E 시각 확인).[^5]
- credit 잔고 상한 100ms 클램프 (유휴 후 폭주 방지).
- ★**금지 2건** (Exp_44 실측 오답 — test_16 이 회귀 감시):[^6]
  1. max-credit 선택 — credit 소비율을 균등화시켜 어떤 목표든 5:5 로 붕괴.
  2. depth1 클라이언트 + 작업보존 폴백 — 재제출 갭이 강제 교대를 만들어 5:5 붕괴.
     클라이언트는 depth≥2 파이프라인 권장(depth1 은 처리율 −15%대).

## §4. 실측 수치 (판정 근거)

| 항목 | 값 |
|---|---|
| 비율 정확도 (30s) | charged 오차 **0.0000~0.0001** (5:5/7:3/3:7 ×3회)[^7] |
| 비율 정확도 (1s 창) | \|오차\| ≤ 0.0014 (1.4ms 입도 무관)[^7] |
| 프록시 고유 오버헤드 | −1.0% (스레드) / **−0.8% (IPC/UDS, 150KB/req)**[^8] |
| E2E (feeder 경유) | 7:3→0.7004, 런타임 3:7 전환→0.3000, 이탈→0.9942, 합류→0.7030[^9] |
| GPU 게이트 대비 | GPU ≤0.007 (Exp_16) — NPU 가 한 자릿수 이상 정밀 (균질 추론시간 특성) |

## §5. K8s 노출 규약 (Exp_47)

**PE 풀 분할** (게이트 1(B)+2(A) — 이중계상 원천 차단, 프록시 상시 기동):

| 리소스 | 대상 PE | 수량 | 경로 |
|---|---|---|---|
| `keti.io/npu-<model>` (예: npu-resnet50) | 프록시 전용 PE (npu0pe0) | **슬롯 10개** | 프록시 대행 (본 문서 §2) |
| `keti.io/npu` | 나머지 PE (npu0pe1) | PE 수 | 직접 배타 할당 (디바이스 세트 부여) |

- **이중계상 금지가 최상위 원칙**: 프록시 PE 는 직접 풀에서 제외된다
  (`--npu-proxy-model` 지정 시 `--npu-proxy-pe` 제외 — npu.go). 같은 PE 가 두
  리소스 이름으로 광고되면 물리 초과 배치가 가능해진다(Exp_31 이중광고 재발 금지).[^12]
- **슬롯 규약**: 프록시 PE 는 다중 테넌트 공유가 목적이므로 수량 1 이 아니라
  슬롯 10개(1 슬롯 = duty 0.1 의향)를 광고한다 — 파드의 슬롯 수가 시간 몫 표현
  (7 슬롯 = 0.7). LSU 비례 배분과 동형. 슬롯 수는 계약 표현이며 실제 집행은
  feeder 등록(arm + time_add)이 담당한다 — 등록은 Exp_53 부터 자동(§6).[^13]
- **모델 리소스 Allocate**: 디바이스 노드를 부여하지 않는다(PE 는 프록시 소유).
  주입 = 프록시 소켓 디렉토리 hostPath 마운트 + env(`KETI_NPU_PROXY_DATA_SOCK/
  TENANT(=canonical 슬롯)/SLOTS/SHARE/MODEL`). 파드는 데이터 프로토콜 클라이언트.[^13]
- **fusion 미지원**: PE 풀이 갈리므로 "수량 2 = fusion" 규약은 성립하지 않는다
  (직접 풀도 1 PE 라 `keti.io/npu: 2` 는 Pending). PE 분할과 fusion 은 원래
  상충(처리량↔지연, Exp_36/43) — 미지원을 명시하고 fusion 이 필요하면 분할 해제
  구성(`--npu-proxy-model` 미지정 = Exp_33 규약)으로 돌아간다.[^14]
- 프록시 컨테이너는 DS 사이드카(상시): ENF 는 hostPath 캐시에서 `--enf` 직접 로드
  (furiosa-models 의존 없음), 네이티브 의존은 이미지에 포함(HAL/컴파일러/onnxruntime —
  pip 휠 미포함이 실측 확인됨).[^15]

### §5-1. PE 풀 배치 옵션 — 다중 모델 vs 직접 경로 (Exp_59)

물리 PE 총량이 고정(Warboy 1기 = PE 2개)이므로 **모델 수와 직접 경로는 서로의
비용**이다. 배치는 셋 중 하나를 고르는 문제이고, 정답은 워크로드가 정한다.[^17]

| 구성 | PE0 | PE1 | 광고 | 얻는 것 | 잃는 것 |
|---|---|---|---|---|---|
| **A. 기본**(현행 라이브) | 프록시(resnet50) | 직접 | `npu-resnet50:10` + `npu:1` | 모델 자유(직접 파드가 임의 모델 적재) | 프록시 모델 1종 |
| **B. 다중 모델** | 프록시(모델 A) | 프록시(모델 B) | `npu-<A>:10` + `npu-<B>:10` | 모델 2종 × 각 10슬롯 시분할 | **직접 경로 상실**(`keti.io/npu` 미광고) |
| **C. fusion** | \-- 분할 해제 --| | `npu:2` | 2-PE 융합(지연 ↓, Exp_36 ×1.40) | 시분할·모델 리소스 전부 |

**B 의 실측 근거** (Exp_59 Phase 2-1, PE0=resnet50 라이브 프록시 + PE1=
efficientnet_b0 실험 프록시, 각 2테넌트 40s):[^17]

- **두 PE 가 독립적으로 시분할된다**: 동시 실행에서 PE0 7:3 → 실측 0.7085/0.2915
  (오차 0.0085), PE1 5:5 → 0.4996/0.5004 (오차 0.0004). 각 PE 의 게이트가 상대
  PE 부하와 무관하게 목표를 지킨다.
- **PE 간 간섭 없음**: 단독 대비 동시 실행의 PE 합산 처리량 PE0 735.0→754.8
  (1.027), PE1 1052.8→1083.2 (1.029) — **감소가 아니라 소폭 증가**. Exp_43 의
  합산 0.961(같은 모델 2-PE 동시)보다 높고, 물리 PE 분리가 실효임을 뒷받침한다.
  (+2.8%는 측정 조건 차이로 보이며 간섭 부재의 방증 — 이득 주장 아님.)
- **시분할 자체 비용은 −0.8%**: PE1 단일 테넌트 독점 1061.6 ips vs 2테넌트 합
  1052.8 ips — Exp_45 의 IPC 오버헤드(−0.8%)와 일치.
- **이중계상 없음**: 두 모델이 서로 다른 물리 PE 를 쓰므로 구조적으로 분리된다.
  코드도 같은 PE 를 두 모델에 지정하면 기동을 거부한다(구성 검증).

**트레이드오프 판단 기준**: 직접 경로의 가치는 "**임의 모델을 파드가 직접 적재**"
(프록시는 기동 시 정한 1모델 고정)이고, 그 대가는 그 PE 를 **1 파드가 독점**
(시분할 불가)하는 것이다. 따라서 — 고정된 소수 모델을 여러 테넌트가 나눠 쓰면
**B**, 모델이 다양하거나 예측 불가면 **A**, 단일 저지연 작업이면 **C**.

**구성 방법**(`--npu-proxy` 반복 플래그, Exp_59 신설):

```
# B 구성 예 — 프록시 2개(각 모델·PE·소켓 디렉토리 분리) + npu-proxy 사이드카 2개
--npu-proxy-model=resnet50 --npu-proxy-pe=npu0pe0 --npu-proxy-dir=/var/lib/kraken/npu
--npu-proxy=model=efficientnet_b0,pe=npu0pe1,dir=/var/lib/kraken/npu-eff
```

전 PE 가 프록시 소유가 되면 `keti.io/npu` 는 **광고 자체를 생략**한다(빈 리소스
광고 금지 — 수량 0 을 광고해 스케줄러를 혼란시키지 않는다).

## §6. 생명주기 (Exp_53 — 자동 등록·해제)

- **등록 (자동)**: 모델 슬롯 Allocate 시 plugin 이 ① 프록시 admin `register`(제어
  소켓 선생성) ② feeder `/feeder/register`(ratio=슬롯/10, resource=npu) ③ `arm`.
  ★등록 실패는 경고 후 진행 — Allocate 실패로 파드 기동을 막지 않는다.
- **해제 (자동)**: watcher 가 checkpoint 의 ResourceName 으로 모델 슬롯 파드를
  식별(디바이스 노드 불요), 파드 소멸 시 canonical 슬롯(정렬 최솟값) 1건만
  release + `/feeder/deregister`(신설 라우트) + 프록시 `unregister` — Exp_41
  위생(N−1 노이즈 금지) 계승. 부분 실패는 로그 후 다음 기회 복구.
- **이탈 재분배 정책**: 기본 **(A) 계약 유지** — 남은 테넌트는 자기 몫 그대로
  (실측: 파트너 이탈 후 15s 창 charged 10.496s = 정확히 0.7×15s, in-envelope 격리
  §4 정합). opt-in **(B)** `--npu-redistribute` — 남은 테넌트 슬롯 비례 재정규화
  (Σ슬롯≤10 을 K8s 가 보장하므로 초과 위험 없음).
- **영속성**: DS 재기동 시 plugin 이 checkpoint 에서 기존 슬롯 파드를 재등록
  (★라이브 파드 필터 — stale checkpoint 엔트리의 고아 재등록을 차단. Exp_53
  E2E 에서 실측된 결함의 수정).
- **클라이언트 재접속 (Exp_55 — 해소)**: 참조 `client.py` 가 연결 끊김을 감지해
  지수 백오프(0.2→3.2s)로 재연결하고 **테넌트 ID 를 유지한 채 핸드셰이크를 재수행**
  한다(프록시 `ensure_tenant` 는 멱등이라 재등록 수용 — 프록시 측 변경 불요).
  ★**재연결 중 요청은 재전송하지 않는다**: 프록시가 무상태 대행이라 진행 중이던
  미결 요청은 유실이 확정이므로 세마포어만 반납해 새 요청으로 대체하고 유실 건수를
  보고한다(중복 추론 방지). 실측: 프록시 강제 재기동 → **파드 재시작 없이 약 1초
  만에 복구**(95→642→749 ips), 재연결 2회·미결 유실 5건. 대조군(`--no-reconnect`)
  은 같은 조건에서 처리량 절반으로 붕괴.[^16]

## §7. 제약 (정직 명시)

- **모델**: runner 당 1모델 고정 — **다른 모델 두 테넌트는 한 PE 에서 불가**.
  같은 PE 이중 runner 는 모델 무관 즉시 거부, 모델 스위칭(close→create→첫 추론)
  3.2~3.7s ≈ 요청 입도의 ~2300배.[^10] 이종 모델은 PE 분리로만.
- **지연 트레이드오프**: 몫 작은 테넌트의 지연이 역비로 증가 — 7:3 에서 3할 쪽
  p50 ≈ 7할 쪽의 2.3배(depth4). strict+depth1 은 p50 낮지만 tick refill 대기가
  p99 꼬리(24ms)를 만든다. 지연 SLA 테넌트는 몫 확대·depth/tick 축소·PE 전용으로.[^11]
- **격리**: 한 프로세스가 다중 테넌트를 대행 — 메모리·오류 격리가 GPU 프로세스
  분리보다 약하다(런타임 오류 전파 가능).[^3]
- **선점 없음**: 실행 중인 1건(~1.4ms)은 비선점 — urgent 류 축은 미구현.
- **다중 NPU (npu1, npu2…) 미검증**: 232 는 Warboy **1기뿐**이라 다중 NPU 는
  **실측이 불가능**하다. PE 식별자가 `npu<N>pe<M>` 형식이라 구성 문법은 이미
  NPU 인덱스에 일반적이지만(`--npu-proxy=model=X,pe=npu1pe0` 이 파싱·검증됨),
  **NPU 간 배치 정책·NUMA/PCIe 지역성·장치별 헬스 처리는 넣지 않았다** — 검증
  없는 추상화를 피한다는 판단이다. 하드웨어 확보 시 (a) 다중 NPU 발견(현
  `DiscoverNPUPEs` 는 이미 npu0~7 스캔), (b) 프록시 사이드카 N개 배치, (c)
  NPU 별 헬스/소실 처리 순으로 확장한다.[^17]

---

[^1]: Exp_36 (reports/Exp_36_npu_partition/report.md — 이중 세션 즉시 거부, API 전무); Exp_43 §Phase 3 재확인 (0.0s 거부, native strings 스캔).
[^2]: Exp_36 (PE=최소 분할 단위); controller/feeder/feeder.py register() 주석 + docs/api/controller_api.yaml `resource` 필드 설명 (Exp_45).
[^3]: Exp_44 §Phase 4 구조 비교표 (reports/Exp_44_npu_request_gate/report.md).
[^4]: Exp_45 §Phase 1·3 (reports/Exp_45_npu_feeder/report.md); runtime/npu_proxy/README.md (프로토콜 명세).
[^5]: Exp_45 §Phase 4 (fig2 — ratios 적용 순간 A 가 B 도착 전 0.7 duty 로 강하); time_sharing.md §4 (in-envelope).
[^6]: Exp_44 §Phase 0 설계 이력 (report.md — 스모크 실측 5:5 붕괴 2건); tests/test_16_npu_gate.py.
[^7]: Exp_44 §Phase 1·2 (report.md — 3비율×3회, analyze_windows).
[^8]: Exp_44 §Phase 2 (701.1 vs 708.4); Exp_45 §Phase 2 (697.3 vs 703.2, 3회).
[^9]: Exp_45 §Phase 4 (logs/e2e_console.log — observed_share 4세그먼트).
[^10]: Exp_44 §Phase 4 (p4_model_probe — busy 거부 0.96s, 스위칭 3.65/3.32/3.16s).
[^11]: Exp_44 §Phase 3 (7:3 지연표 — wc/d4 18.6ms, strict/d1 p99 24.3ms).
[^12]: Exp_47 게이트 1 (reports/Exp_47_npu_k8s_expose/report.md); device-plugin/npu.go NewNPUPlugin(excludePE) + npu_model_test.go TestExcludeProxyPE.
[^13]: Exp_47 §Phase 1 (npu_model.go 슬롯 규약 주석); E2E 실측 — 7/3 슬롯 파드 → feeder 7:3 → charged 0.7003/0.2997 (report.md §Phase 3).
[^14]: Exp_47 게이트 1 fusion 재검토; Exp_36 (fusion↔PE 처리량/지연 상충).
[^15]: Exp_47 §Phase 2 — libfuriosa_hal/컴파일러/libonnxruntime 부재 크래시 실측 → 이미지 포함 (이미지 745MB).
[^16]: Exp_55 §Phase 2 (reports/Exp_55_debt_closure/report.md — 라이브 프록시 강제 재기동 실측, exp50 재확인).
[^17]: Exp_59 §Phase 2 (reports/Exp_59_reboot_npu_scale/report.md — PE0 라이브 프록시 + PE1 실험 프록시 동시 실측 3세그먼트 ×40s, 라이브 DS 무변경 경로); device-plugin/npu_proxy_cfg.go + npu_proxy_cfg_test.go (구성 파서·이중계상 거부·전 PE 프록시 시 직접 광고 생략).
