# NPU 게이트 v2 — RNGD 2계층 설계서 (Exp_69)

- **지위**: 설계서. 구현 없음 — 구현·실증은 후속 Exp(§D-4 지정)에서.
- **입력 근거**: 의미론 개정 `docs/semantics/npu_gate.md` §0, 실측
  `reports/Exp_67_rngd_survey/`·`reports/Exp_68_rngd_partition/`·
  `reports/rngd_verify_20260803/`(외부 검증 4차수).
- **표기 규약**: [실측] = 근거 있는 사실, [가정] = 미검 — 검증 Exp 지정.

## 0. 왜 2계층인가

Warboy 프록시는 "PE 의 유일 runner 를 소유하고 추론 1건(~1.4ms)을 배분"하는
단일 계층이었다. RNGD 에서는 실행 소유권이 furiosa-llm serve(프로세스당 PE 그룹
정적 점유, 내부 continuous batching)로 넘어갔고 모델 로드가 6.2s[실측]라
"실행을 우리가 소유하고 건건이 배분"하는 그림 자체가 성립하지 않는다.
남는 우리 몫은 두 가지다 — **어느 테넌트에게 어느 PE 그룹을 줄 것인가**(공간,
벤더 스택이 안 함), **테넌트 요청이 serve 에 어떤 순서·양으로 들어가게 할
것인가**(시간, 벤더 스케줄러는 테넌트 개념이 없음).

```
테넌트 A ─┐                       ┌─ furiosa-llm serve #1 ── npu:1:0-3
테넌트 B ─┼─ [계층② 요청 게이트] ─┤
테넌트 C ─┘   (테넌트 몫·우선순위)  └─ furiosa-llm serve #2 ── npu:1:4-7
                                        ▲
              [계층① PE 그룹 할당자] ────┘  (그룹 배정·회수·EBUSY 중재)
```

## D-1. 계층 ① — PE 그룹 할당자 (공간)

**역할**: 테넌트/모델 요구 → fusion 입도(1/2/4/8 정렬 블록) 그룹 배정, 회수,
EBUSY 사전 회피(이중 배정 차단), 그룹별 회계.

**배정 규칙**:
- 단위 = 정렬 블록만 [실측: fusion {1,2,4,8}, 임의·비정렬 조합 불가 — Exp_67 B-4].
  즉 배정 상태공간은 {8×1PE, 4×2PE, 2×4PE, 1×8PE 및 정렬 혼합(예: 4PE+2PE+2PE)}.
- 메모리는 입도에 비례해 하드 파티션 [실측: PE당 5.94GiB, 초과 요청은 그룹 내
  거절·침범 불가]. → **회계가 단순해진다**: 그룹 배정 = 연산+메모리 동시 확정,
  Warboy 시절의 "메모리 쿼터 별도 집행" 축이 NPU 에서는 불필요.
- 이종 모델 동거 제약[실측]에 따라, 현 아티팩트 생태계에서는 "그룹 2개 = 같은
  모델 2벌 또는 (모델 A tp4 + Qwen2.5-0.5B tp≤4)" 만 성립함을 배정기가 알아야
  한다 — 모델별 tp 요구는 `reports/rngd_verify_20260803/rngd-verify/phase4/`
  의 매트릭스·판정 스크립트(rngd_model_check.sh)를 데이터 소스로 쓴다.

**K8s 정합 (핵심 난점)** — 카드 단위 광고 vs 그룹 단위 회계:

| 안 | 내용 | 성립 조건 | 비용 |
|---|---|---|---|
| (가) **카드 수령·내부 분할** (권고 초안) | 표준 furiosa device plugin 이 광고하는 `furiosa.ai/rngd=1` 을 **우리 관리 파드 1개**가 통째로 받고, 그 안에서 계층①이 그룹을 나눠 serve 들을 기동 | 표준 스택 무변경. 외부 검증의 "분할은 파드 내부에서" 결론과 일치[실측: plugin 에 분할 옵션 없음] | K8s 스케줄러에게 그룹이 안 보임 — 그룹 회계는 우리 계층이 자체 노출(메트릭/API)해야 함. kueue quota 도 카드 단위(rngd=1)로만 잡힘 |
| (나) PE 그룹 단위 광고 (자체 plugin) | Warboy 방식 계승 — 우리 plugin 이 `keti.io/rngd-pe` 류로 8개(또는 그룹) 광고 | 표준 furiosa plugin 과 **이중 광고 충돌 해소 필요**(같은 카드를 두 이름으로 — Exp_31 금지 원칙 위반 소지). 표준 DS 를 밀어내는 협의 필요(엣지 과제 공유 노드) | 디바이스 노드 세트 구성(DMA ch 16개 전부 마운트 필수[실측 — 부분 마운트 실행 실패])·plugin 개발/운영. 카드가 1장뿐이라 광고 세분화의 즉각 이득 작음 |
| (다) DRA | K8s 1.34+ DRA 드라이버 | 외부 검증에서 "할당 단위 동일(카드), alpha, 이점 없음" 판정[실측] | 현 시점 배제 |

**권고**: (가) 로 시작한다. 232 는 엣지 과제와 공유라 표준 스택(gpu-operator 식
furiosa-npu-operator)을 밀어낼 수 없고[Exp_64], kueue edge-cq 가 `furiosa.ai/rngd`
quota 를 이미 카드 단위로 잡고 있다. 그룹 수준 가시성은 계층①이 controller
API(`/npu/groups` 류)와 메트릭(`furiosa_npu_*`, core="0-3" 라벨 — 외부 검증의
에이전트 프로토타입 스키마 재사용)으로 제공한다. (나)는 RNGD 전용 노드가 생기고
그룹 임대가 실제 다중 파드 수요가 될 때 재평가. [가정 A3 — kueue 와의 정합 상세는
미검, 검증 Exp 지정 §D-4]

**회수**: 프로세스 사망 시 디바이스 해지 1.0ms [실측 — SIGKILL 후, 외부 검증].
→ 계층①은 "죽였는데 EBUSY 잔존" 시나리오에 유예 로직을 둘 필요가 없다.
serve 재기동 비용(로드 6.2s+)이 지배하므로, 회수의 실비용은 해지가 아니라
**재기동**이다 — 그룹 재배정은 드물게, 계획된 창에서만 [가정 A2: 런타임 재배정
불가(재기동 필요)를 전제로 설계 — 미검, §D-4].

## D-2. 계층 ② — 요청 게이트 (시간)

**위치**: furiosa-llm serve **앞단** (OpenAI 호환 HTTP 리버스 프록시 형태).
테넌트 식별 → 테넌트별 큐 → deficit 스케줄링으로 serve 에 방출(admission).
serve 내부의 토큰 레벨 배치는 벤더 스케줄러에 위임한다[실측: 버킷 패킹형
continuous batching, 테넌트·우선순위 개념 없음 — Exp_68 §6].

**gate_core 재사용/폐기 (파일 단위, 코드 실사 기반)**:

| 파일/모듈 | 판정 | 사유 |
|---|---|---|
| `runtime/npu_proxy/gate_core.py` | **재사용 (거의 무수정)** | 디바이스 무관 설계(run_fn 주입) — Tenant/armed/credit/granted/charged, deficit 선택(`min(charged/granted)`), strict 의미론, 100ms 클램프 전부 요청 단위로 유효. 변경점: ① 회계 단위 — run_fn 실측 벽시계(us)를 "요청 점유 시간"으로 재정의할지 "생성 토큰 수"로 바꿀지 결정 필요[가정 B2] ② 동시성 — Warboy 는 step() 직렬 1건, LLM 은 serve 가 동시 처리하므로 방출 동시성 한도(세마포어) 추가 |
| `runtime/npu_proxy/npu_proxy.py` — 제어 평면(`_ctrl_loop`/`_admin_loop`, UDS DGRAM `time_*`) | **재사용** | feeder 계약(Exp_16/26 명령 계열·`total= kernels=` 회계 발화)이 그대로 이식됨 — controller/feeder 무변경 재사용이 목적 |
| 〃 — runner 소유부(`create_runner`, `--enf`, `IN_SHAPE`, `_run_req`, warmup sanity) | **폐기** | `create_runner`/ENF 는 RNGD 세대에 소멸[실측 Exp_67]. sanity gate **패턴**(워밍 후 100ms 초과 시 기동 중단)만 신설 코드에 계승 |
| 〃 — 데이터 평면(고정 shape 텐서 프레이밍, `_conn_loop`/`recv_exact`) | **폐기** | LLM 요청은 OpenAI 호환 HTTP — 고정 텐서 프로토콜 부적합. 신규는 HTTP 프록시 |
| 〃 — 스케줄러 루프(`_sched_loop`) | **일반화** | "step() 반복" 골격은 유지하되 방출형(요청을 backend 로 forward, 완료 콜백에서 회계)으로 재작성 |
| `runtime/npu_proxy/client.py` | **폐기 (의미론만 계승)** | 텐서 프로토콜 클라이언트 — LLM 은 표준 openai SDK 가 클라이언트. 단 재접속 의미론(재연결 시 **미결 요청 재전송 금지** — 중복 추론 방지, Exp_55)은 게이트→serve 연결 단절 처리 규칙으로 문서 계승 |
| `runtime/npu_proxy/README.md` | 보존 (구세대 표기) | 프로토콜 명세는 Warboy 기록 |
| `device-plugin/npu.go`·`npu_model.go`·`npu_proxy_cfg.go` (+tests) | **보류 (D-1 (가) 채택 시 미사용)** | Warboy PE 풀 분할 광고 규약(슬롯 10개 등) — (가)안에서는 광고 주체가 표준 plugin 이라 미사용. (나)안 재평가 시 일반화 후보. 코드 정밀 판정은 구현 Exp 에서 |
| `tests/test_16_npu_gate.py` | **재사용** | gate_core 회귀 감시(금지 2건 — max-credit·작업보존 폴백) — 코어를 재사용하므로 그대로 유효 |

**벤더 노브와의 관계 [가능성 — 실증은 후속 Exp]**: `--npu-queue-limit`(기본 1),
`--max-num-batched-tokens`, `--max-batch-size` 는 게이트가 방출량을 조절할 때
serve 측 수용 한도로 작용한다. 게이트 동시성 한도와 `npu_queue_limit` 의 곱이
실효 in-flight 를 결정할 것으로 보이나 **미검**[가정 B3]. `--scheduler-kind` 의
named 구현 목록은 미확인.

**무효화된 수치**: 구세대 "IPC 오버헤드 −0.8%" 는 Warboy 추론 1.4ms 대비 값 —
RNGD 요청(수십 ms~s)에서는 **재산정 필요**. HTTP 프록시 홉 오버헤드로 재정의된다.

## D-3. 현행 npu_proxy 존폐 요약

위 표의 판정 열 집계: **재사용** gate_core.py·제어 평면·test_16 / **일반화**
스케줄러 루프 골격 / **폐기** runner 소유부·데이터 평면·client.py /
**보류** device-plugin NPU 3종(안 (가)에서는 미사용) / **보존(기록)** README.

## D-4. 전제와 미검 항목 (검증 Exp 지정)

| # | 항목 | 상태 | 검증 방법 (지정) |
|---|---|---|---|
| A1 | PE 그룹 분리 시 간섭 −1.0%·처리량 ×1.71 | [실측] 외부 검증 3차 §3 | (완료 — 인수) |
| A2 | **PE 그룹 재배정은 런타임 불가, serve 재기동 필요** | [가정] | **Exp_70(가칭) PoC**: 가동 중 serve 의 디바이스 축소/확장 시도 → 불가 확인 후 재기동 시간(로드 포함) 실측 |
| A3 | (가)안이 kueue edge-cq quota 와 충돌하지 않음 | [가정] | 관리 파드를 pool-lq/edge-lq 어느 쪽으로 내는지 포함해 K8s 정합 Exp |
| B1 | **요청 게이트가 TTFT SLO 를 실제로 움직인다** (몫 7:3 이 TTFT 에 반영) | [가정 — 설계의 존재 이유] | **Exp_70 PoC 핵심 측정**: 외부 검증 3차 방법론(probe 32tok×30회 + 배경 동시성 16×75s) 재사용, 게이트 on/off 비교 |
| B2 | 회계 단위(벽시계 vs 토큰 수)로 몫 제어가 성립 | [가정] | PoC 에서 두 회계로 각각 7:3 목표 → 실측 배분 오차 비교 |
| B3 | 게이트 동시성 × `npu_queue_limit` 이 실효 in-flight 결정 | [가정] | 노브 스윕 (PoC 부속) |
| B4 | 같은 모델 다중 테넌트 몫 제어 성립 | [가정] | B1 과 동일 실험에서 판정 |
| C1 | SR-IOV 불가 | [실측 — 사고] | 벤더 펌웨어 수정 전 재검증 금지 |
