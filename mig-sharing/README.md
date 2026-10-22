# KETRIS MIG 내부 공유

하나의 **기존 MIG compute instance**를 Kubernetes 확장 자원으로 노출하고, 각 컨테이너에 MPS 활성 스레드 상한과 별도의 시간 weight를 적용한다. SM 상한은 전용 SM 소유권이나 VRAM 하위 파티션이 아니다. 시간 비율은 협력 런타임이 계상한 커널 경과 시간의 비율이며 GPU exclusive busy time이 아니다.

구조: kubelet Allocate → immutable allocation journal → Pod UID/컨테이너/checkpoint/CUDA 실제 UUID 검증 → MIG별 MPS와 10ms budget domain → atomic 관측 export. Webhook은 UUID·공간 수량·독립 time-weight를 검증하며 MIG 오류를 거부한다. UUID가 다른 장치로 대체되는 fallback은 없다.

초기 지원 환경: Linux x86_64, CUDA 12.9 direct CUDA launch, CDI를 지원하는 containerd, host systemd, GI당 CI 하나. 기본 MPS와 동일 UID 0으로 실행하는 협력 컨테이너를 사용한다. 임의 CUDA API·PyTorch 그래프·악의적 런타임 우회에 대한 강제 격리는 제공하지 않는다. MIG initContainer/envFrom은 초기 지원 범위 밖이다. 기존 NPU PE 플러그인과 독립적으로 동작한다.

CUDA 프로그램은 공유 libcudart로 링크해야 한다(`nvcc --cudart shared`). 정적 libcudart 실행 파일은 후킹 검증 범위 밖이며 초기 gate/metadata 검사가 없으면 실행 성공으로 분류하지 않는다.

```bash
bash mig-sharing/tools/build.sh
bash mig-sharing/tools/deploy.sh --dry-run=server
```

운영자가 유휴 장치에 MIG를 준비한 뒤 `nvidia-ctk cdi generate`로 해당 UUID가 포함된 NVIDIA CDI 명세를 준비한다. `ketris-mig-config`의 `allowlist`에 실제 UUID를 넣고 대상 node에 `ketris.re.kr/mig-runtime=true` 라벨을 지정한다. 빈 allowlist는 아무 MIG도 관리하지 않는다. 전체 GPU LSU 플러그인에는 이 브랜치의 MIG 제외 코드가 필요하고, Webhook에는 이 이미지의 검증 코드와 named ConfigMap get 권한이 필요하다. 새 이미지로 plugin·webhook 컨테이너를 업데이트하며 기존 controller/feedback/advertiser는 유지할 수 있다.

```bash
python3 -m ketris_mig.manifest --mig-uuid MIG-실제UUID --space-percent 50 \
  --time-weight 3 --image 협력-CUDA-이미지 -- /workload
```

실제 resource key는 UUID SHA256 앞 16자로 결정되며 manifest 도구가 NVML에서 찾아 넣는다. `limits=requests=50`은 MIG 전체 대비 MPS 50% 상한이고 `ketris.re.kr/time-weight: "3"`은 같은 MIG의 다른 워크로드 weight와 비교해 시간 예산을 정한다. 최대 200개 논리 단위를 광고하므로 합계 공간 상한이 100%를 넘을 수 있다. 메모리는 MIG 전체 메모리를 공유하므로 모델 적재 합계가 별도로 들어맞아야 한다.

노드 `/var/lib/ketris-mig/{inventory.json,allocations,bindings,partitions}`에 요청·적용·실측 기록을 분리해 저장한다. MPS는 `ketris-mps-*.service`로 host에서 실행되어 runtime Pod 재시작에도 유지된다. 런타임 종료는 시간 예산을 0으로 하고, workload가 종료된 뒤에만 소유한 서버를 정리한다. 외부 `/tmp/nvidia-mps`를 종료하는 운영 명령은 런타임에 없다.

하드웨어 검증과 복원은 `tools/verify_live.py`를 사용한다. 보고서는 원본 입력, actual CUDA UUID, SM, 누적 커널/시간, 반복 구간, 재시작과 정리 상태를 포함한다. 결과가 없는 조건을 통과로 표시하지 않는다.

2026-10-13 실제 검증: `reports/ketris_mig_runtime_20261013_full`의 5초 예열+20초×3 측정에서 solo132.501425 iter/s, 공간50:50 합계128.472752 iter/s와22/22 SM, 시간3:1 A74.9561093%, 결합 A74.9383409%·합계62.283738 iter/s를 기록했다. 두 MIG의 실제 CUDA UUID/device_count1/독립10000us 예산, override 거부도 확인했다. 첫 재시작은 in-place 라이브러리 설치로 실패했고, atomic 설치 수정 후 `reports/ketris_mig_runtime_20261013_recovery`에서 MPS PID 유지·Pod binding 복원·커널 재개를 확인했다. 두 보고서의 복원 기록은 MIG Disabled/CDI/기존 MPS/전체GPU LSU240·NPU PE8 복원을 확인한다.

설치 스크립트는 별도 `ketris-mig-admission`을 기존 Webhook CA/Service로 구성한다. MIG 요청/limit에만 실패 시 거부 정책을 적용하며, 다른 Pod의 기존 정책을 유지한다. geometry 변경 후에는 core LSU 플러그인을 재시작해 NVML 초기 inventory를 갱신한다. runtime 라이브러리는 비교 후 atomic 교체해 실행 중인 mmap을 보존한다.
