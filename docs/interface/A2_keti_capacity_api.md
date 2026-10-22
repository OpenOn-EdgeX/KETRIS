# A-2. KETI → 고려대 : 가용 용량 조회 API

- **방향**: KETI(자원 분할) → 고려대(스케줄러)
- **목적**: 고려대 스케줄러가 제출 전 노드의 **가용 용량·총 할당률·간섭 구간**을 조회.
- **상태**: KETI 제안. 엔드포인트 경로·집계 단위는 협의 대상.

## 엔드포인트 판단: **새 엔드포인트** `/capacity` (근거)

기존 controller REST(8090)는 `/status`(SHM 상태)·`/feeder/*`(제어 내부)를 노출하나, 이들은
**KETI 내부 표현**(virtual_sm, tick, budgets)이라 고려대에 그대로 주면 결합도가 높다. 따라서:

- **새 엔드포인트 `/capacity`를 추가**해 외부용 안정 계약으로 분리한다(내부 `/status`·`/feeder/status`를
  집계해 구성). 내부 표현이 바뀌어도 `/capacity` 계약은 유지.
- 데모/스텁에서는 라이브 controller를 건드리지 않기 위해 **별도 프로세스(포트 8091)**로 shim을 띄운다.
  프로덕션에서는 controller(8090)에 `/capacity`를 흡수한다.

## 응답 스키마 (`GET /capacity`)

```json
{
  "schema_version": "1.0",
  "node": "gpu-npu-server-02",
  "devices": [
    {
      "uuid": "GPU-xxxx",
      "kind": "gpu",
      "model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
      "capacity": {
        "physical_lsu": 178,             // 물리 용량(실측 LSU)
        "advertised_lsu": 178,           // 광고 용량(=물리×초과할당계수)
        "overcommit_factor": 1.0,        // 초과 할당 계수
        "allocated_lsu": 0,              // 현재 총 할당
        "allocation_ratio": 0.0,         // 현재 총 할당률 = allocated/physical (=Σpct)
        "lsu_measured": true             // ★실측 LSU인가, 잠정값인가
      },
      "memory": { "capacity_mb": 97887, "quota_divisor": "advertised_lsu" },
      "slices": [                        // 슬라이스별 현재 몫
        { "tenant": "keti-gpu0-lsu89", "compute_pct": 50.0, "mem_mb": 48000, "time_ratio": 0.5, "armed": true }
      ],
      "interference_hint": {             // ★간섭 구간 판정 힌트 (Exp_78~80 실측)
        "current_sigma_pct": 0.0,        // 현재 겹침 = allocation_ratio
        "no_interference_below": 1.00,   // Σpct ≤ 100% : 공간분할 격리, 무간섭
        "interference_above": 1.40,      // Σpct ≥ 140% : 실경합(지켜줄 작업의 처리량이 떨어진다)
        "note": "100~140%는 지켜줄 작업의 무게에 달린 전이구간. 가벼운 저지연 작업은 ~130%까지 안전"
      }
    }
  ],
  "npu": {
    "available_cores": 8,
    "request_rule": { "allowed_blocks": [1, 2, 4, 8], "alignment": "power_of_two_contiguous" }
  }
}
```

## 필드 규약

- `allocation_ratio` = `allocated_lsu / physical_lsu` = **Σpct**(겹침 정도). 이 값이 간섭 판정의 1차 축.
- `overcommit_factor`가 1.0을 넘으면 `advertised_lsu > physical_lsu`. 고려대는 `advertised_lsu`까지 admit
  가능하나, `allocation_ratio`가 `interference_above`를 넘으면 지연민감 워크로드 배치를 피해야 한다.
- `lsu_measured=false`면 그 장비의 용량은 **잠정값** — 고려대는 이를 신뢰도 낮음으로 취급(A-1 measured 전파).
- `interference_hint`는 **판정 힌트일 뿐 SLO 결정이 아니다**. SLO 알고리즘은 고려대 몫.

## 확인 필요 (고려대 협의)

1. 고려대 스케줄러가 폴링(주기 GET)을 쓰는가, watch/구독을 원하는가.
2. `interference_hint`의 임계(100/140%)를 고려대가 그대로 쓸지, 지켜줄 작업의 무게별 곡선(A-3 유형 + Σpct)을
   직접 받아 자체 판정할지.
3. 집계 단위 — 노드 단위 응답이면 충분한가, 디바이스별이 필요한가(현재 디바이스 배열로 제공).
