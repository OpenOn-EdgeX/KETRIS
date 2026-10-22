# 고려대 ↔ KETI Controller 인터페이스 스펙 — 초안 (Exp_30 작업 3)

> **문서 지위: 기술회의 안건용 제안 초안이다. 기관 간 합의 전이며, 합의된 계약이
> 아니다.** 엔드포인트는 시그니처 수준 제안이고 구현이 아니다. 확정 시
> `docs/api/controller_api.yaml` 에 정식 등재한다.

## 1. 역할 경계 (워크샵 합의 문구 재확인)

**고려대 = 요청·배치 레벨 / KETI = 커널·operator 레벨(SM 비율·시간 슬라이스)**
(PROJECT_CONTEXT.md:27, 워크샵 합의 문구). 프로파일링도 동일 경계 — 고려대는
워크로드 프로파일링, KETI 는 커널 레벨 프로파일링.

이 초안에서의 해석:
- 고려대: LSU 총량 산정·노드 선택·큐잉·SLO 라우팅 (어느 노드에 몇 LSU를 보낼지)
- KETI: 노드 내 s×t 분해·집행 (받은 LSU 를 SM 비율×시간 슬라이스로 어떻게 나눌지),
  실측 점유·이벤트 보고

## 2. KETI 가 노출할 것 (제안 시그니처)

### 2-1. 가용 capacity 조회 — ledger 기반
```
GET /capacity
→ 200 {
    "node": "<node-name>",
    "gpus": [ { "index": 0, "model": "NVIDIA RTX PRO 6000 ...",
                "lsu_total": 178,            // lsu/catalog.json 기준
                "lsu_free_estimate": <int>,  // ledger free_sm/free_time 환산
                "free_sm_ratio": 0.5,        // 장부 기준 (Exp_26 §3)
                "free_time_ratio": 0.3 } ]
  }
```
- 근거 데이터는 현행 `GET /lifecycle/ledger` 의 view (free_sm_ratio/free_time_ratio)
  와 LSU 카탈로그(`lsu/catalog.json`)의 결합. LSU 환산 규칙(공간·시간 잔여를 하나의
  LSU 수로 접는 방식)은 **미합의 — 회의 안건 A-1**.

### 2-2. 할당 요청/응답
```
POST /allocate
  { "request_id": "<uuid>", "lsu": 13,
    "hint": { "workload_class": "PREFILL|DECODE|CNN_TRAIN|...",  // 선택
              "confidence": "HIGH|LOW" } }
→ 200 { "feasible": true, "provisional": false,
        "decision": { "space_ratio": 0.3, "time_ratio": 0.4, "limit_pct": 30 },
        "warnings": [ "..." ] }        // 회피 권고 — 거부 아님 (아래 A-2)
→ 200 { "feasible": false, "reason": "INFEASIBLE", ... }
```
- `feasible/INFEASIBLE` 판정과 (s,t) 분해는 현행 ratio 엔진 `decide()` 경로
  (Exp_25, `POST /lifecycle/swap` 에서 이미 사용)를 재사용한다.
- `provisional` 플래그: LSU 카탈로그 값이 provisional 상태(기관 확정 전 —
  catalog.json `status` 필드)인 동안 응답에 명시.

### 2-3. 메트릭·이벤트
```
GET /capacity/occupancy        // 실측 점유 보고
→ 200 { "observed_share": {...}, "charged_delta_us": {...},
        "metric_definition": "charged_us = 배치 벽시계 창 점유 (GPU busy 아님)" }

이벤트 (구독/푸시 — 전달 방식 미합의, 회의 안건 A-3):
  TERM_PREDICTED   테넌트 종료 예측 (Exp_24/28 — 감지 지연 ~1s 급)
  PHASE_TRANSITION 국면 전환 (Exp_19 — prefill/decode 등)
  FALLBACK         swap 폴백 (FALLBACK_GATED 진입)
```
- **점유 보고의 charged 정의 명기 (필수)**: 점유·SLA 지표로 쓰이는 `charged_us` 는
  **배치 벽시계 창 점유이며 GPU busy 가 아니다**. 동종 페어에서는 두 정의가
  일치(갭 0.001)하지만 이종 페어(prefill×decode)에서는 체계적으로 갈라진다
  (갭 0.096). 정본 정의: `docs/semantics/time_sharing.md` **§1** 참조.
  보고 지표를 소비하는 고려대 측 SLO 판정 로직은 이 정의 차이를 전제해야 한다.

## 3. KETI 가 받을 것

- **워크로드 힌트**: 분류(workload_class)가 있으면 전달 — KETI ratio 엔진의 (s,t)
  분해 입력이 된다. 없으면 KETI 가 자체 커널 레벨 프로파일링으로 분류한다
  (Exp_13 커널 분류 엔진, 역할 경계 §1과 일치).
- 할당 해제 통지 (또는 KETI 종료 감지 이벤트에 대한 확인 응답 — 방식 미합의, A-3).

## 4. 미해결 안건 목록 (기술회의 논의용)

| # | 안건 | 배경 |
|---|---|---|
| A-1 | LSU 환산 규칙: 공간·시간 잔여를 단일 LSU free 수치로 접는 방식 | ledger 는 (free_sm, free_time) 2축 — 1차원 LSU 로의 사영 규칙 필요 |
| A-2 | **PREFILL+PREFILL 류 회피 권고의 처리 주체**: KETI 는 warnings 로 전달만 하고 거부하지 않는다(현행 v1, Exp_26 ledger — Exp_29 이연 안건). 배치 거부/재배치 결정은 누구인가? 요청·배치 레벨이면 고려대 소관이 자연스러우나 미합의 | Exp_25/26 이연 |
| A-3 | 이벤트 전달 방식 (파일 tail / HTTP push / 메시지 버스) 및 해제 통지 방향 | 현행은 노드-로컬 events 파일 구독 |
| A-4 | **SLA 지표 정의 합의**: charged(창 점유) vs GPU busy vs 처리량 — 이종 혼합에서 서로 다른 값(§2-3). 공인시험·SLO 판정에 어느 것을 쓰는가 | Exp_29 §3 |
| A-5 | **urgent 요청의 발신 주체**: `POST /urgent` 는 노드-로컬로 존재. 요청 레벨 SLO 위반 감지(고려대) → urgent 발신인가, KETI 자체 판단인가. 발신 시 R-1 규칙(time_credit 동반 전환, semantics §2) 준수 책임 포함 | Exp_27 §5 |
| A-6 | LSU 카탈로그 값 확정 (provisional 해제) — "LSU 정의·네이밍·값 확정 (Option C 기반)" 공통 안건 | PROJECT_CONTEXT.md:191 |

## 5. 비고

- 본 초안의 KETI 측 근거 기능은 모두 현행 코드 실재 경로다: ledger
  (`GET /lifecycle/ledger`), ratio 엔진(`decide()`), 점유 실측(`GET /feeder/occupancy`),
  이벤트(TERM_PREDICTED — Exp_24/28). 신규 구현 대상은 §2 의 표면(라우트) 뿐이다.
- 배포 관련: Device Plugin LSU 모드 매니페스트는 `device-plugin/deploy/daemonset-lsu.yaml`
  (배포는 HAMi 공존 정책 협의 후 — 매니페스트 헤더 참조).
