# KETRIS 기관 연계 시험 어댑터

`ketris.integration.v1`은 기관 공식 protobuf 호환을 주장하지 않는 제안 계약이다. 연대 프로파일과 성대 결정은 SYNTHETIC 입력이며 통신/후단 CUDA는 실제 검증한다. 수신 receipt는 승인이나 적용 ACK가 아니다.

별도 `ketris-integration` 제어 namespace, `ketris-integration-workloads` 실행 namespace, 전용 CQ와 두 AdmissionCheck를 사용한다. 협업 코드·공유 큐는 수정하지 않는다. 첫 검증은 MIG 내부 FP32 GEMM이며 LLM/NPU 새 추론은 포함하지 않는다.

계약 생성: `.venv/bin/python institution-bridge/tools/generate_proto.py`

검사: `.venv/bin/python -m unittest discover -s institution-bridge/tests -v` (실제 localhost 소켓 허용 필요).

빌드: `.venv/bin/python institution-bridge/tools/build.py --compile`. 두 신규 이미지만 통합 VM에서 빌드/import한다. 고정 wheel은 Python3.12용으로 받는다. worker는 Allocate가 제공하는 CUDA library mount에 의존한다.

배포는 먼저 `tools/preflight.py --out <evidence-dir>`로 실제 target과 경쟁 작업을 기록하고, 그 target으로 render한 manifest만 `tools/deploy.py`로 dry-run/apply한다. 소유권이 다른 동일 이름 리소스는 거부한다. 토큰은 운영자가 별도 Secret `bridge-auth`의 `token` key로 제공하며 로그에 출력하지 않는다.

## 현재 통합 VM 설치

서버 `ketris-institution-bridge:20261015-v5`, worker `ketris-institution-gemm:20261015-v1`. 내부 주소는 `ketris-institution-bridge.ketris-integration.svc:50051`이다. 클라이언트 Secret 참조는 `bridge-auth/token`이며 값은 출력하지 않는다.

상태·순서 이벤트는 서버 Pod에서 인증 파일을 사용해 읽는다.

```bash
kubectl -n ketris-integration exec deploy/ketris-institution-bridge -- \
  python3 -m ketris_integration.client --workload institution-grpc-20261015-04-a state
kubectl -n ketris-integration exec deploy/ketris-institution-bridge -- \
  python3 -m ketris_integration.client --trace trace-institution-grpc-20261015-04 events
```

`tools/run_e2e.py`는 CPU 부정 검증, 실제 GPU 실행, 서버 재시작·동일 요청 재전송을 별도 phase로 수행한다. 실행 전 preflight가 쓴 실제 `target.json`을 새 증거 폴더에 둔다. 새 run-id를 사용하고 이전 완료 Job을 삭제하지 않는다. GPU phase는 host의 native JSON export 읽기가 필요하므로 통합 VM에서 `KUBECONFIG`를 명시한 운영자 권한으로 실행한다. 다른 GPU 작업이 있으면 쿼터를 해제하지 않는다.

프로파일 메모리1GiB는 합성 요구량이고 host RAM128Mi와 다르다. SM22는 실제 CUDA 보고값이며 MPS50%의 전용 코어 격리를 의미하지 않는다. 시간 weight3/1은 둘이 활성일 때7500/2500µs이고 단독 활성 시10000µs도 정상이다. 계상시간은 CUDA kernel wall time이다.

공식 기관 .proto 호환, 기관 최적화 알고리즘, 자동 MIG Router 분류, RPM 과금, 새 LLM/NPU 추론은 이번 검증에 포함하지 않는다. 협업 코드와 공유 큐는 변경하지 않는다. 입력/receipt/상태는 PVC SQLite에 보관하며 커널·크레딧 이벤트마다 API 서버에 쓰지 않는다.

`tools/verify_result.py`로 실제 원문·UID·두 승인·카운터·종료·멱등성을 재검산한 뒤에만 `archive.make_replay_bundle`과 `tools/build_slide.py`를 사용한다. `tools/capture_logs.py`는 원본 SHA256을 대조하는 stdlib 콘솔 뷰어이며 새 실행을 가장하지 않는다.

대시보드 `http://10.0.1.211:31080/`에서 **실험 재생 → 기관 GRPC · MIG · 최종 V5 · 2026.10.15**를 선택한다. 3개 측정 창과 원본 증빙을 제공한다. 실시간 조회에서 devmaster의 **2026-10-15 18:24:40 KST**를 선택하면 실제 실행 당시 A/B와 다른 MIG의 연결 없음이 보인다. 기존 재생 UI는 보수적으로 전체 연결 목록을 미기록으로 표시하지만, 새 원본 `windows.json/all_start`에는 다른 MIG의 zero-tenant 관측도 보존했다. 이번 범위에서 frontend는 변경하지 않았다.

로컬 증거 폴더 `.../institution-grpc-e2e/institution-grpc-20261015-04`에 대해 `python3 capture_logs.py <증거폴더> --section all`을 실행하면 캡처용 실제 기록 필드 요약을 표시한다. 원본 로그는 `kubectl -n ketris-integration-workloads logs job/ki-ce1b5524165e328db71c`(A), `... job/ki-4a3e3f074f616caadb54`(B)에서 조회한다. mock client 원본은 `kubectl -n ketris-integration logs job/yonsei-institution-grpc-20261015-04-a` 및 `... job/skku-institution-grpc-20261015-04-a`다. 완료 Job과 MIG는 유지했다.
