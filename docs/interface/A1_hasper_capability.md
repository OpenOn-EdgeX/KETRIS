# A-1. 하스퍼 → KETI : 물리 자원 명세 (capability JSON)

- **방향**: 하스퍼(HW 관리) → KETI(자원 분할)
- **목적**: KETI가 물리 장비를 논리 용량(LSU/PE)으로 환산·광고하기 위해 필요로 하는 **최소 물리 정보**.
- **상태**: KETI 제안. 하스퍼가 실제로 실을 수 있는 필드 범위는 협의 전 — 아래는 우리가 **필요로 하는 최소 집합**이고 선택 필드는 열려 있다.
- **★전달 경로 (Exp_94 개정)**: **`RemotePool` CRD 가 정본**이고 파일 드롭 JSON 은 legacy 경량 경로다. 아래 §CRD 정본 참조.

> ## ★ 하스퍼에 물을 것 (1순위, 협의 전제)
> **회수를 예고할 수 있는가, 즉시 회수만 가능한가?**
> 이 답이 상태 모델 전체를 가른다:
> - **예고 가능**(`reclaim_pending` 전이 지원): 아래 회수 핸드셰이크(드레인→광고 축소→`reclaim_ready`)가 성립. KETI가 슬라이스를 안전하게 비운 뒤 회수 확정. **우리 감시 데몬은 이 모델로 구현됨(Exp_82 실증).**
> - **즉시 회수만 가능**: `reclaim_pending` 개념이 성립하지 않고 **슬라이스 드레인 시간이 없다**. 이 경우 KETI 쪽에 **강제 종료 경로**(진행 중 슬라이스 즉시 kill + 광고 즉시 축소)가 필요하며, 회수 중 워크로드는 유실을 감수한다. 상태 필드는 `allocatable`/`reclaimed` 2단으로 축소된다.
>
> **답이 정해지기 전까지 아래 3단 상태 모델은 잠정이다.**

## 전달 방식 — ★Exp_94 개정: **CRD 정본**, 파일 드롭은 legacy

> **개정 이력**: Exp_81 은 파일 드롭을 택했고(아래 근거 유효), Exp_91 이 CRD 를 병행 권장,
> **Exp_94 에서 CRD 를 정본으로 확정**했다(회수 핸드셰이크가 CRD 경로에 구현되면서).
> 아래 파일 드롭 근거는 **삭제하지 않고 보존** — legacy 경로의 설계 근거로 여전히 유효하다.

**현행: `RemotePool` CRD (`remote_pool/crd_remotepool.yaml`) 가 정본.** →§CRD 정본
표준 오브젝트라 kubectl 가시성·RBAC·watch·status 서브리소스·conditions 를 공짜로 얻고,
회수 핸드셰이크에 필요한 **spec/status 소유권 분리**가 자연스럽다.

### (legacy) 파일 드롭 — Exp_81 결정 근거, 보존

HTTP POST가 아니라 **파일 드롭**(약속된 경로에 capability JSON 파일 배치)을 택한다.

- 근거 1: capability는 **저빈도·상태성** 데이터(장비 구성 변경·회수 시에만 바뀜)라 실시간 push가 불필요.
- 근거 2: 파일은 **재기동에 강함** — KETI device-plugin이 재시작해도 파일을 다시 읽으면 복구(현 카탈로그도 CM 파일 기반).
- 근거 3: 하스퍼 쪽 구현 부담이 최소(HTTP 서버 불요, 파일 쓰기만).
- 경로: `/var/lib/kraken/capability/<device_uuid>.json` (KETI가 감시). 회수는 파일 상태 필드로 신호.
- (선택) 실시간성이 필요해지면 동일 스키마를 HTTP POST로도 받을 수 있게 A-2 controller에 엔드포인트를 열 수 있다.

## 스키마 (`capability.schema.json` 참조)

```json
{
  "schema_version": "1.0",
  "device": {
    "uuid": "GPU-xxxxxxxx",              // 필수: 전역 고유 식별
    "kind": "gpu",                        // 필수: "gpu" | "npu"
    "model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",  // 필수: 카탈로그 매칭 키
    "vendor": "nvidia",
    "remote": false                       // 필수: 원격(CXL/PCIe fabric) 자원 여부
  },
  "compute": {
    "units": 188,                         // 필수: 연산 유닛 수 (GPU=SM, NPU=PE)
    "unit_kind": "sm",                    // "sm" | "pe"
    "concurrency": {                      // 필수: 동시 실행 지원
      "mps": true,                        // GPU MPS 가용
      "mig": false,                       // GPU MIG 가용
      "partition_granularity_pct": 1      // pct 분할 최소 단위
    }
  },
  "memory": {
    "capacity_mb": 97887,                 // 필수
    "bandwidth_gbps": 1792                // 선택(있으면): 대역폭
  },
  "npu": {                                // kind=="npu"일 때만
    "cores": 8,                           // 코어(PE) 수
    "mem_per_core_mb": 6082,              // 코어당 메모리
    "fusion": {                           // 묶음 제약
      "allowed_blocks": [1, 2, 4, 8],     // 허용 묶음 크기 목록
      "alignment": "power_of_two_contiguous"  // 정렬 규칙
    }
  },
  "status": "allocatable",                // 필수: allocatable | reclaim_pending | reclaimed
  "measured": true,                       // 선택: KETI 실측 LSU 존재 여부 힌트(없으면 KETI가 판단)
  "extra": { }                            // 확장: 하스퍼 고유 필드 자유 영역
}
```

## 필드 규약

- `device.uuid`: GPU는 CDI UUID(`nvidia.com/gpu=<uuid>`)와 동일해야 KETI가 디바이스 부여와 매칭한다.
- `status` 전이(회수 핸드셰이크, Part B):
  - `allocatable` → 정상 광고 대상.
  - `reclaim_pending` → KETI가 해당 디바이스 슬라이스를 비우고 광고를 축소(신규 admit 중단).
  - `reclaimed` → KETI가 광고에서 완전 제거 후 하스퍼에 확인(파일 삭제 또는 ack 파일).
- `npu.fusion.allowed_blocks`: KETI는 이 목록 밖 크기를 Allocate 단계에서 거부한다(현 RNGD {1,2,4,8}).
- `memory.bandwidth_gbps` 부재 시: KETI는 대역폭 기반 판정을 건너뛴다(간섭 힌트는 Σpct로만).


## ★ CRD 정본 (Exp_94) — 회수 핸드셰이크 구현 완료

**정본 = `remote_pool/crd_remotepool.yaml` (`RemotePool`, `keti.re.kr/v1alpha1`).**
Exp_91 이 CRD 를 "권장"으로 올렸으나 **회수는 파일 드롭 경로에만 구현**돼 있었고, CRD 는
`status.phase` enum 만 선언하고 컨트롤러가 쓰지 않아 **항상 `Allocatable`** 이었다(선언만·미구현).
Exp_94 에서 CRD 경로에 회수 핸드셰이크를 구현해 **정본으로 확정**한다.

### 소유권 분리 (표준 K8s: spec=desired / status=observed)

| 필드 | 누가 쓰는가 | 뜻 |
|---|---|---|
| `spec.reclaim: bool` | **provider(하스퍼 자리)** | 이 풀을 회수하겠다는 **의도** |
| `spec.reclaimGracePeriodSeconds: int` | **provider** | 드레인 유예. `>0`=예고형, **`0`=즉시형** |
| `status.phase` | **KETI 컨트롤러** | `Allocatable` / `ReclaimPending` / `Reclaimed` |
| `status.conditions[]` | KETI | 표준 conditions — `Advertising` / `Draining` / `Reclaimed` |
| `status.observedGeneration` | KETI | 컨트롤러가 반영한 spec 세대 |
| `status.reclaimStartedAt` / `reclaimCompletedAt` | KETI | 회수 지연 측정(정량지표 해지시간) |
| `status.leases[]` | KETI | 점유의 진실 원천(`podNamespace` 포함 — 강제 드레인용) |

### 전이

```
Allocatable ──(spec.reclaim=true)──▶ ReclaimPending ──(leasedLsu==0)──▶ Reclaimed
     ▲                                     │                                │
     └──────(spec.reclaim=false)───────────┴────────────────────────────────┘
                                    (회수 취소 / 재광고)
```

- **`ReclaimPending` 진입 즉시 광고를 `leasedLsu` 로 축소한다** — 여유분이 사라지므로 **신규 유입이 차단**되고,
  기존 lease 는 계속 돈다. lease 가 반납될 때마다 광고를 더 줄인다(`DRAIN_SHRINK`).
- 유예 초과 시 lease 보유 파드를 `--force --grace-period=0` 으로 종료(`ReclaimForced`).

### ★ 하스퍼의 "회수 예고 가능?" 답을 기다리지 않아도 된다

A-1 §확인필요 2 는 상태 모델 전체를 가르는 질문이었으나, **유예 정책 하나로 두 분기를 같은 코드가 덮는다**:

| 하스퍼 능력 | 쓰는 값 | 동작 |
|---|---|---|
| 회수 **예고 가능** | `reclaimGracePeriodSeconds: 60` | 드레인 대기 → 자연 반납 → 회수 |
| **즉시 회수만** 가능 | `reclaimGracePeriodSeconds: 0` | 대기 없이 강제 종료 → 회수 |

따라서 협의 결과가 어느 쪽이든 **필드 값만 다르게 쓰면 되고 구현 변경이 없다.** 질문은 여전히 유효하나 **더 이상 블로커가 아니다.**

### 실측 (Exp_94)

| 시나리오 | 유예 | 결과 | 회수 소요 |
|---|---|---|---|
| 예고형 | 60s | 기존 lease 자연 종료 대기 후 회수 | **13.83s**(파드 잔여 수명이 지배) |
| 즉시형 | 0s | 600s sleep 파드를 강제 종료 후 회수 | **2.52s** |
| 회수 취소 | — | `Reclaimed` → `Allocatable`, 광고 29 재개 | 즉시 |

- 유입 차단 실증: `ReclaimPending` 중 여유분(15 LSU) 요청 파드는 `Insufficient keti.re.kr/remote-lsu` 로 **Pending**.
- Event 발행 확인: `ReclaimStarted` / `ReclaimForced` / `ReclaimReady` (A-4 규격).

### 안전장치 (capability_watcher 규약 계승)

- **광고 축소만 자동.** `spec.compute.units` 가 커져도 자동 확대하지 않고 로그만 남긴다(`UNITS_GROW_IGNORED`).
- CR 조회 실패·손상 → **현 광고 유지**(0 으로 만들지 않는다).
- 파드 목록 조회 실패 → **반납 판정 보류**(조회 실패를 반납으로 오판하지 않는다).

### 파일 드롭 경로 (legacy)

`/var/lib/kraken/capability/<uuid>.json` + `integration_demo/keti_bridge/capability_watcher.py` 는
**계속 동작하되 legacy** 다. 하스퍼가 CRD 를 쓸 수 없을 때의 경량 대안으로만 유지한다.
신규 기능(유예 정책·conditions·회수 지연 측정)은 **CRD 경로에만** 넣는다.

---
## 확인 필요 (하스퍼 협의)

1. capability에 **실제로 실을 수 있는 필드 범위** — 특히 `memory.bandwidth_gbps`, `remote`, MIG 지원 여부를 하스퍼가 알 수 있는가?
2. ~~**회수 예고 가능 여부**~~ — **더 이상 블로커 아님**(Exp_94). 두 분기를 `reclaimGracePeriodSeconds` 값으로 덮었고 강제 종료 경로도 구현됐다. 다만 **어느 값을 쓸지**는 여전히 하스퍼가 알려줘야 한다(예고 가능하면 60, 즉시뿐이면 0).
3. `device.uuid`가 CDI/드라이버 UUID와 일치하는가 (매칭 키 확정).
4. 파일 드롭 경로·권한 — `/var/lib/kraken/capability/`를 하스퍼 프로세스가 쓸 수 있는가.
