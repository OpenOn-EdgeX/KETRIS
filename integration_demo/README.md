# KRAKEN 통합 데모 패키지 (Exp_81, 2026-08-11)

> **이 패키지의 스텁 3종은 각 기관 구현으로 대체됩니다.** KETI가 제안하는 인터페이스가 실제로
> 맞물리는지 보이는 최소 증거입니다. **우리가 제안하는 규격이지 확정이 아닙니다** — 각 기관의
> 실제 구현 제약에 따라 조정 여지가 있습니다. 협의 항목은 각 규격 문서의 "확인 필요" 절과 아래 참조.

인터페이스 규격 정본: `../docs/interface/` (A-1~A-4).

## 구성

| 자리 | 스텁 | 대체 기관 | 역할(최소) |
|---|---|---|---|
| HW 명세 | `hasper_stub/` | 하스퍼 | 232 GPU/NPU → A-1 capability JSON 드롭 |
| 자원 분할 | `keti_bridge/` | **KETI(우리, 대체 아님)** | capability 등재 + A-2 `/capacity` 서빙 |
| 〃 webhook | `keti_bridge/webhook/` | **KETI(우리)** | A-3 annotation→env mutating webhook (Exp_82, `failurePolicy: Ignore`) |
| 〃 감시데몬 | `keti_bridge/capability_watcher.py` | **KETI(우리)** | A-1 회수 핸드셰이크 자동화 (reclaim_pending→드레인→reclaim_ready) |
| 스케줄/제출 | `korea_stub/` | 고려대 | A-2 조회 → A-3 유형 부여(annotation만) → 제출 |
| 관측 | `orchestro_stub/` | 오케스트로 | A-4 Prometheus exporter(+유지율) + K8s Event + Grafana JSON |

> **webhook 안전**: `failurePolicy: Ignore`(죽어도 파드 생성 안 막힘) + `namespaceSelector: kraken-webhook=enabled`(데모 NS만, 시스템 NS 제외) + `timeoutSeconds: 5` + pods CREATE만.
> 롤백 = `kubectl delete mutatingwebhookconfiguration kraken-annotation-webhook` 한 줄. TLS 인증서(SAN IP)는 로컬 생성(`webhook_config.tmpl.yaml`의 caBundle placeholder 참조).

## 실행 방법

```bash
# 0) 전제: kraken device-plugin/controller(8090) 라이브, kraken-infer:opt 이미지, MPS 가동
export KRAKEN_CAP_DIR=/var/lib/kraken/capability
kubectl create namespace kraken-demo

# 1) KETI 브리지(capacity API) + 오케스트로 exporter 상시 기동
python3 keti_bridge/keti_bridge.py &          # :8091 /capacity
python3 orchestro_stub/kraken_exporter.py &     # :9095 /metrics

# 2) 간섭 구간 시연을 위해 초과 할당 계수 임시 상향 (데모 후 1.0 복원 필수)
#    catalog overcommit_factor=1.5 → allocatable 267

# 3) 한 줄기 데모
bash run_demo.sh
```

## 의도적으로 안 만든 것 (§0 원칙)

- 하스퍼: PCIe/CXL 통신, fabric 발견, 실제 회수 HW 제어 — status 필드 신호만.
- 고려대: SLO 알고리즘, 우선순위 스케줄링, 실제 라우팅 정책 — "총 할당률 X 이상 회피" 자리표시만.
- 오케스트로: Prometheus 서버, Grafana, 대시보드 UX, 알람 — exporter 엔드포인트 + 대시보드 JSON만.
- KETI 브리지: annotation→env mutating webhook — 프로덕션 경로만 문서화, 스텁은 submitter가 직접 세팅.

## 협의 시 확인할 것 (기관별)

- **하스퍼**(A-1): capability에 실을 수 있는 필드 범위, 회수 예고(reclaim_pending) 가능 여부, uuid=CDI UUID 일치.
- **고려대**(A-3): 워크로드 유형을 스케줄러가 채울 수 있는지, pipeline-stage↔class 매핑 주체, SLA 계약 지표.
- **오케스트로**(A-4): Prometheus 수집 경로 사용 가능 여부(현재 미설치, DCGM exporter만), 필요한 집계 단위.
