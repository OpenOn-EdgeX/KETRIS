# KETRIS

GPU·NPU 워크로드의 자원 할당, GPU/MIG 내부 공유, 시간 크레딧 제어와 관측을 위한 Kubernetes 통합 소스입니다.

이 저장소는 2026-10-22 개발 상태를 보존하는 초기 스냅샷입니다. 기존 PRISM/KRAKEN 식별자는 호환성을 위해 일부 코드·환경 변수·이미지 경로에 남아 있습니다. 배포 매니페스트와 실행 스크립트에는 기존 실험 환경의 경로·이미지 태그가 있으므로 대상 환경에 맞춰 검토해야 합니다.

## 구성

| 경로 | 역할 |
|---|---|
| `controller/`, `shm/` | 자원 제어와 공유 메모리 계약 |
| `runtime/` | CUDA 런타임 후킹, 시간 크레딧, NPU 요청 게이트 |
| `device-plugin/`, `webhook/`, `lsu/` | Kubernetes 자원 광고·할당 및 워크로드 설정 주입 |
| `mig-sharing/` | MIG 인스턴스 선택, MPS 공간 상한, MIG별 실행 제어 |
| `institution-bridge/` | 기관 간 프로파일·할당 계획·승인 연계 gRPC 계약 |
| `dashboard/` | 실시간 상태 조회와 실험 재생 |
| `profiler/`, `monitor/` | 워크로드 특성 분석과 관측 |
| `integration_demo/`, `examples/`, `tests/` | 연동 예제와 검증 코드 |
| `experiments/seven-workload-20261022/` | 7개 LLM 워커 비교 실험의 실제 원본·분석 코드·실행 당시 런타임 소스 |

MPS의 active-thread percentage는 실행 자원 상한입니다. 전용 SM의 하드웨어 격리나 독점 할당을 뜻하지 않습니다. KETRIS 시간 크레딧의 계상 값과 NVML GPU activity 역시 서로 다른 지표입니다.

## 시작점

- [MIG 공유 경로](mig-sharing/README.md)
- [대시보드](dashboard/README.md)
- [기관 연계](institution-bridge/README.md) 및 [Protobuf 예제](docs/integration-examples/institutions-v1/README.md)
- [NPU 요청 게이트](runtime/npu_proxy/README.md)
- [실제 7개 워커 실험](experiments/seven-workload-20261022/README.md)
- [출력 형식용 합성 예제](examples/synthetic-console/README.md)

## 개발 환경과 빌드

Python 패키지 의존성은 각 `pyproject.toml`, Go 버전은 각 `go.mod`, 프런트엔드 의존성은 `dashboard/frontend/package-lock.json`에 기록되어 있습니다. CUDA 빌드는 NVIDIA 드라이버와 호환되는 CUDA Toolkit 및 C++17 컴파일러가 필요합니다. 기존 네이티브 probe의 `sm_120` 설정은 Blackwell 실험 환경 기준입니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./mig-sharing -e ./dashboard/backend -e ./institution-bridge
npm --prefix dashboard/frontend ci
npm --prefix dashboard/frontend run build
```

일반 빌드 산출물과 모델 가중치는 포함하지 않습니다. 기존 base-image Dockerfile은 `device-plugin/image/lib/`의 네이티브 라이브러리를 입력으로 받습니다. 소스에서 재빌드한 후 이미지를 준비해야 합니다.

```bash
make -C runtime/libbless CUDA_HOME=/usr/local/cuda
make -C runtime CUDA_HOME=/usr/local/cuda
make -C profiler/collector
mkdir -p device-plugin/image/lib
cp runtime/libbless/libbless.so runtime/gating_lib.so device-plugin/image/lib/
cp profiler/collector/kernel_tracer.so device-plugin/image/lib/
```

`mig-sharing/Dockerfile`의 기본 base image(`kraken-device-plugin:exp161`)와 배포 이미지 태그는 기존 환경의 값입니다. 새 환경에서는 준비한 이미지로 변경해야 합니다. `tools/build_image.sh`, `mig-sharing/tools/build.sh`, `dashboard/tools/build.sh`는 이미지 빌드 후 로컬 containerd로 가져오는 작업까지 수행합니다. 단순 소스 확인용 명령으로 실행하지 마세요.

## 실험 데이터와 출처

실험 원본의 `SHA256SUMS.txt`는 당시 파일 922개의 무결성을 확인합니다. 실제 측정은 예비 6회와 본 실험 15회(3조건 × 5회)이며, 매회 7개 Python 워커가 동일한 총 56개 요청을 처리했습니다. 67.1% 출력은 `examples/synthetic-console/`의 합성 예제이며 실측 결과와 분리되어 있습니다.

실험 당시 런타임은 `experiments/seven-workload-20261022/runtime-source/`에 별도 보존했습니다. **현재 `runtime/libbless/` 개발 소스와 동일하지 않습니다.** 현재 소스를 빌드한 결과를 과거 실험 실행 바이너리로 간주하면 안 됩니다.

소스 출처는 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json), 이번 게시 검증 범위는 [VALIDATION.md](VALIDATION.md)를 참조하세요. 개발 중이던 변경도 포함한 파일 스냅샷이며 기존 저장소 이력 전체를 가져온 것은 아닙니다. 원래 저장소와 과거 대형 로그는 로컬에 보존했습니다.

과거 dashboard archive/store 테스트 일부는 별도로 보관된 10월 12~13일 보고서에 의존합니다. 이 초기 스냅샷에서 모든 과거 하드웨어 실험을 다시 검증했다고 주장하지 않습니다.
