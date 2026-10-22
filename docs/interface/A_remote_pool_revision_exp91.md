# 연동 규격 개정판 (Exp_91) — 원격 자원 풀링 반영

> Exp_81~83의 4계층 규격(A-1~A-4)을 **원격 자원 + 표준 K8s** 기준으로 개정. 실측 근거: `reports/Exp_91_remote_pool/`.
> ⚠️ **1GbE 제약**: 성능 수치(오버헤드 ~2ms 등)는 1GbE 제어페이로드 기준. **대역폭 특성은 하스퍼 fabric에서 재측정 필요.** fabric으로 일반화 금지.

## A-1 개정 — 파일 드롭 JSON → CRD (병행)

- **현행 유지**: 파일 드롭 `/var/lib/kraken/capability/<uuid>.json` 은 하스퍼가 계속 써도 됨(경량 경로).
- **추가(권장)**: **`RemotePool` CRD**(`keti.re.kr/v1alpha1`, `reports/Exp_91_remote_pool/manifests/crd_remotepool.yaml`).
  표준 오브젝트 → kubectl 가시성·RBAC·watch·status 공짜. **필드는 A-1 그대로**(발명 없음):
  | A-1 capability.schema.json | RemotePool CRD |
  |---|---|
  | `device.{uuid,kind,model,remote}` | `spec.device.{uuid,kind,model,remote}` (★`remote` 이미 A-1에 존재) |
  | `compute.{units,unit_kind,concurrency}` | `spec.compute.{units,unitKind,concurrency}` |
  | `memory.capacity_mb` | `spec.memory.capacityMb` |
  | `measured` | `spec.measured` + `measuredNote`(원격지연 포함 명시) |
  | `status`(allocatable/reclaim_pending/reclaimed) | `status.phase`(Allocatable/ReclaimPending/Reclaimed) |
  | (신규) 점유 진실원천 | `status.leases[]` + `leasedLsu` + `advertisedLsu` |
  | (신규) 제공 호스트·게이트 | `spec.providerHost`, `spec.gateUrl` |
- **lease = 점유의 진실 원천**: 소비 노드(232)에 원격 디바이스가 물리적으로 없어 kubelet 카운트 불신 → 풀 발급 lease 를 CR status 에. **명시적 반납 경로**(파드 종료 폴링, Turnaround Booster 계열) — TTL-only 는 파드 종료 후 자원 유휴. 실측 반납: 폴링 0.5s 이내 감지.

### DRA 전환 경로 (3차년도)

| RemotePool CRD (2차년도) | DRA (3차년도) | 남는 것 |
|---|---|---|
| `RemotePool` CR | `ResourceSlice`(풀 광고) + `DeviceClass` | device/compute 필드 그대로 이관 |
| `spec.compute.units` | ResourceSlice `devices[].capacity` | 값 보존 |
| `status.leases[]` | `ResourceClaim` + `allocation` | lease→claim 의미 동일 |
| 노드 확장리소스 패치(현) | DRA 스케줄러 플러그인 | 수동 패치 소멸 |
| `spec.gateUrl` 주입 | `ResourceClaim` `status.devices[].data`(opaque) | 게이트 배선 그대로 |
- CRD→DRA 는 **필드 재명명 수준**(스키마 보존). DRA 착지 전까지 CRD 가 표준 오브젝트 이점을 제공.

## A-2 개정 — 가용 용량에 로컬/원격 구분

- `/capacity` 응답 `devices[]` 에 `device.remote`(bool) 이미 존재 → 소비자가 로컬/원격 구분 가능.
- 원격 device 는 `capacity.units` 가 **원격지연 포함 실측치**임을 `measured_note` 로 명시(silicon peak 아님).
- 총 가용 = 로컬 Σ + 원격 Σ(풀별). 예: 232 = 로컬 178 LSU(Pro6000) + 원격 29 LSU(5090@233) + NPU 8 PE.

## A-4 개정 — 로컬/원격 메트릭 + DCGM 판정 반영

- **로컬/원격 라벨**: 모든 slice/node 메트릭에 `location="local|remote"` + 원격은 `provider` 라벨(`kraken_exporter.py` 반영). 원격 풀은 `kraken_node_advertised_lsu{location="remote"}`, `kraken_remote_leased_lsu`, `kraken_remote_pool_phase`.
- **★DCGM 판정(Exp_90 §7 반영)**: "디바이스 관측 점유"를 두 층으로 분리 —
  - **device 총점유 = DCGM**(하드웨어 실측, `DCGM_FI_DEV_GPU_UTIL/FB_USED`). 라벨 gpu/UUID.
  - **per-slice 점유 = kraken 회계**(charged-time/유지율). MPS 하 DCGM per-process 귀속 불가(Exp_75/90).
  - 원격은 DCGM 이 233 측(제공 호스트)에서 device 총점유, per-slice 는 232 게이트 charged-time.

## 각 기관 확인 요청 (갱신)

- **하스퍼**: capability 를 CRD(RemotePool)로 발행 가능한지 / 파일드롭 병행 여부. **fabric 대역폭 재측정**(1GbE 값 일반화 불가). 회수 상태모델(즉시 vs 예고).
- **고려대**: 원격 자원 요청 시 `keti.re.kr/remote-lsu` 확장리소스 사용. 워크로드=요청 단위(작은 페이로드)만 원격 적합.
- **오케스트로**: `location` 라벨·`kraken_remote_*` 메트릭 반영. device 총점유는 DCGM, per-slice 는 kraken 회계로 표기 분리.
