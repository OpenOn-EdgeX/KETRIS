# 연대·성대 gRPC 데이터 예시

이 디렉터리는 검토용 자료 구조와 더미 데이터다. 기관의 공식 `.proto`, 실제 프로파일 실측값, 배포된 gRPC 서비스 또는 장치 실행 결과가 아니다.

목표는 프로파일링 → 성대의 MIG 결정 → KETI 승인 → KETRIS 실제 적용까지 같은 워크로드의 데이터를 연결하는 것이다. 현재 요청 범위는 KETI 플랫폼 검증과 두 기관의 입력 예시 작성이다. 카탈로그·모니터링 제공은 위즈온텍 역할로 보고 제외한다. 이 계약에는 KETI의 카탈로그 조회 RPC가 없다.

## 호출과 파일

`.proto`의 패키지는 제안용 `ketris.integration.example.v1`이다. 기존 협업 저장소의 proto·서버·스케줄러·큐 설정은 수정하지 않는다.

| 방향 | 제안 RPC | 요청 메시지 | 응답 메시지 |
|---|---|---|---|
| 연대 → KETI | `ProfileIngestService.PushProfile` | `PushProfileRequest` | `ProfileReceipt` |
| 성대 → KETI | `PlanAdmissionService.SubmitPartitionPlan` | `SubmitPartitionPlanRequest` | `PlanReceipt` |

| 파일 | 내용 |
|---|---|
| `institution_examples.proto` | 메시지·서비스·RPC 정의 |
| `yonsei-slide-profile.textproto` | 연초 자료의 YOLO 예시, cores=80·mem=4000의 단위 미확정 상태 보존 |
| `yonsei-llm-a.textproto`, `yonsei-llm-b.textproto` | LLM A/B의 합성 프로파일 예시 |
| `skku-plan-a.textproto`, `skku-plan-b.textproto` | 성대가 같은 가상 MIG를 선택한 A/B 배치 결정 |
| `profile-receipt.textproto`, `plan-receipt.textproto` | 제안된 수신 응답 예시, 실제 RPC 응답이 아님 |
| `verify_examples.py` | 컴파일·직렬화·필드 연결·범위 오류 검증, 네트워크 호출 없음 |

## 자료 구조

모든 요청의 `meta`는 `schema_version`, `request_id`, `trace_id`, `workload_id`, `provider`, 데이터 출처, 실행 모드, 생성·만료 시각을 담는다. 재전송은 같은 요청 ID와 내용을 유지하고, 새 프로파일·재계획은 새 버전으로 구분하는 계약을 제안한다. 멱등성 저장·재처리는 향후 서버 구현 책임이며 이 예시 검증 스크립트가 서버 동작을 제공하지 않는다.

연대 요청은 원문에 대응하는 `model`, `type`, `input_shape`, `gpu_req`와 추가 제안인 모델 revision·dtype·batch·프로파일 ID/버전 등을 담는다. LLM 예시의 입력 shape `[1,128]`은 토큰 ID 입력 텐서를 가정하며 입력·출력 토큰 수를 별도로 기록한다. `dtype=fp16`은 모델·연산 정밀도이고 토큰 ID 입력의 `input_dtype=int64`와 구분한다. 전체 모델 입력 사전·토크나이저 계약은 모델별 추가 합의 대상이다. 이번 배치 예시는 앞서 합의한 GPU/MIG 워크로드를 중심으로 한다.

원문의 `cores=80`을 SM 수나 MPS 80%로 해석하지 않는다. `mem=4000`도 바이트 단위를 추정하지 않는다. `optional` 필드와 단위 enum을 사용해 미설정·단위 미확정을 구분한다. 이 원문 fixture는 수신·보관 예시이며 실행용 요구량 변환에 사용할 수 없다.

LLM fixture의 모델은 `Qwen/Qwen2.5-0.5B-Instruct`이며 `fixture-v1`은 실 모델 커밋이 아니다. 2048 MiB, 40 ms, 25 request/s는 모두 시험용 가정이다. 모델을 내려받거나 추론해 얻은 수치가 아니다.

성대 요청에는 `plan_id/revision`, 참조한 `profile_id/version`과 결정한 단일 `assignment`를 넣는다. 후보 목록을 KETI가 다시 점수화하도록 전달하지 않는다. KETI의 목표 역할은 제출된 결정을 검증해 승인·보류·반려하고 사유를 반환하는 것이다.

`assignment.target`은 `oneof`다. 예시에서는 `fixture_target.fixture_ref="fixture-mig-1"`만 사용한다. 미래의 `live_mig_target`에는 실제 node/GPU/MIG UUID·자원 키·인벤토리 세대가 필요하다. 더미 파일에는 실제처럼 보이는 UUID를 만들지 않는다.

`external_snapshot`은 외부에서 제공된 스냅샷의 출처·ID·세대·유효기간을 보존하는 참조다. 예시 출처는 `external-monitor-fixture`이며 실제 위즈온텍 모듈을 호출하거나 모니터링 서비스를 제공하지 않는다.

| 워크로드 | 참조 프로파일 | 성대의 대상 결정 | 공간 상한 | 시간 가중치 |
|---|---|---|---:|---:|
| `wl-llm-a` | `profile-llm-a-v1`, version 1 | `fixture-mig-1` | 50% | 3 |
| `wl-llm-b` | `profile-llm-b-v1`, version 1 | `fixture-mig-1` | 50% | 1 |

공간 상한은 MIG 내부 MPS 상한이며 전용 SM 격리를 뜻하지 않는다. 두 워크로드가 모두 활성인 10 ms 주기의 계획 예산은 7500/2500 µs다. 실제 시간 점유율은 별도 실측이 필요하다. 정책 출처는 `synthetic-test-policy`로, 성대의 파티션 결정 권한과 공간·시간 정책 생산 책임을 구분한다.

## 응답·검증 경계

`ProfileReceipt.RECEIVED`는 프로파일 수신을, `PlanReceipt.PLAN_ONLY`는 합성 계획만 보관한다는 상태를 표현한다. gRPC `OK`, 데이터 수신, KETI 정책 승인, Kueue 실행 허가, KETRIS 실제 적용과 실행은 각각 다르다. 실제 Pod·MIG·런타임·측정값은 이 입력 fixture와 수신 응답에 채우지 않는다.

필드 번호·서비스 이름·자료형은 제안이다. 실제 기관과의 바이너리 호환은 기관의 `.proto`를 받아 합의해야 한다. `optional`은 값의 유무를 보존할 뿐, 범위·권한·버전·만료·모의 실행 방지를 자동으로 검증하지 않는다. 향후 수신 서버가 이를 검증해야 한다.

예시 시각은 2026-10-15 00:00 UTC로 고정했고 유효기간은 60초다. 현재 실행할 수 있는 승인이나 최신 관측을 의미하지 않는다. 실 통신 시험에서는 새 요청 시각·ID와 유효한 실제 참조를 발급해야 한다.

기존 환경의 `grpcio-tools`, `protobuf`를 사용한 예시 검증:

```bash
python3 docs/integration-examples/institutions-v1/verify_examples.py
```

이 명령은 임시 디렉터리에서 Protobuf·gRPC 코드를 생성하고 7개 fixture를 파싱·바이너리 왕복 직렬화한다. 프로파일 연결과 동일 가상 MIG의 3:1 가중치를 확인하고, 모의 모드의 실제 장치 대상·101% 상한·누락된 프로파일 버전을 거부한다. 서비스 기동·실제 RPC 송수신·Kueue 승인·장치 제어 검증은 수행하지 않는다.
