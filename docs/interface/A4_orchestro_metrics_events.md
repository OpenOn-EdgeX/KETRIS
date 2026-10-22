# A-4. KETI → 오케스트로 : 메트릭·이벤트 규격

- **방향**: KETI(자원 분할) → 오케스트로(관측/대시보드)
- **목적**: 오케스트로가 노드·파드 단위로 집계·시각화할 수 있게 메트릭·이벤트 형식을 고정.
- **상태**: KETI 제안. 라벨·수집 경로는 협의 대상.

## 클러스터 현황 (Exp_81 실측)

- **DCGM exporter 가동 중**(gpu-operator, `nvidia-dcgm-exporter`, ClusterIP :9400) — GPU 물리 메트릭 제공.
  단 device-level(프로세스 귀속 없음).
- **Prometheus·Grafana 미설치.** → KETI exporter는 **Prometheus 노출 형식**(text exposition)으로 내되,
  Prometheus 서버 설치는 오케스트로/운영 몫(본 데모는 exporter 엔드포인트까지). Grafana 대시보드는
  **JSON 산출물만**(스크린샷 없음 — 없는 도구를 있는 것처럼 만들지 않음).

## 메트릭 (Prometheus text exposition, `/metrics`)

### 노드 단위

```
# HELP kraken_node_physical_lsu 물리 논리 용량(실측 LSU)
# TYPE kraken_node_physical_lsu gauge
kraken_node_physical_lsu{node="gpu-npu-server-02",device="GPU-xxxx"} 178
kraken_node_advertised_lsu{node="...",device="..."} 178
kraken_node_overcommit_factor{node="...",device="..."} 1.0
kraken_node_allocation_ratio{node="...",device="..."} 0.0          # =Σpct, 간섭 축
kraken_node_interference_zone{node="...",device="..."} 0           # 0=무간섭(<100%) 1=전이 2=간섭(≥140%)
kraken_node_npu_cores_available{node="..."} 8
```

### 슬라이스(파드) 단위

```
# TYPE kraken_slice_compute_pct gauge
kraken_slice_compute_pct{node="...",tenant="keti-gpu0-lsu89",pod="vic-off",class="memory"} 50.0
kraken_slice_mem_mb{...} 48000
kraken_slice_time_ratio{...} 0.5
kraken_slice_armed{...} 1                # 1=제어 걸림(armed) 0=풀림(released)
kraken_slice_control_mode{...,mode="relaxed"} 1   # relaxed|strict|off
kraken_slice_retention_ratio{...} 0.84   # 지켜줄 작업의 처리량 ÷ 단독 실행 시
```

**★ 노드 단위 vs 슬라이스 단위 명시** (오케스트로 집계용):
- `kraken_node_*` = 노드/디바이스 집계값(총 할당률, 간섭 구간).
- `kraken_slice_*` = 파드(테넌트)별 값(몫, 제어 상태, 유지율). 라벨 `pod`·`tenant`로 롤업.

## 이벤트 (Kubernetes Event)

`kubectl get events`로 노출. `reason`·`involvedObject`·`message`:

| reason | involvedObject | 발생 시점 | message 예 |
|---|---|---|---|
| `SliceCreated` | Pod | 슬라이스 생성 | "compute_pct=50 mem=48000MB tenant=..." |
| `SliceDestroyed` | Pod | 슬라이스 소멸 | "tenant=... released" |
| `ControlModeSwitched` | Pod | 제어 방식 전환 | "off→relaxed (지켜줄 쪽 보호 시작)" |
| `InterferenceDetected` | Node | Σpct≥140% 진입 | "allocation_ratio=1.49 ≥ 1.40 — 경합 구간" |
| `ControlAxisFailed` | Node/Pod | 제어 축 실패 | "MPS pipe 소멸 — 공간 제어 무효" |

## Grafana 대시보드 (JSON 산출물)

`integration_demo/orchestro_stub/grafana_dashboard.json`. 패널:
1. **총 할당률 게이지** (0~200%, 100%·140% 임계선 표시)
2. **슬라이스 점유 맵** (테넌트별 compute_pct 스택)
3. **지연민감 워크로드 유지율 추이** (`kraken_slice_retention_ratio`, class="memory")
4. **제어 적용 상태** (armed/mode 테이블)
5. **제어 축 상태등** (공간/메모리/시간/분류/NPU/접근경계 — axis_liveness 6축)

## 확인 필요 (오케스트로 협의)

1. **Prometheus 수집 경로 사용 가능 여부** — 오케스트로가 Prometheus를 운영하는가, 아니면 자체 수집기인가.
   (현 클러스터엔 Prometheus 미설치, DCGM exporter만.)
2. **필요한 집계 단위** — 노드·파드 외에 테넌트 그룹·큐 단위 롤업이 필요한가.
3. K8s Event 보존/수집 방식 — event exporter를 오케스트로가 두는가.
4. exporter 스크레이프 대상 등록 방법(ServiceMonitor CR 등).
