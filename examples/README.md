# PRISM 사용 예제 (Exp_164 — 전부 232 에서 실측 검증됨)

사용 설명은 **`docs/PRISM_USAGE.md`** 를 본다. 이 폴더의 매니페스트는 그 가이드의
예제 원본이며, 각 파일이 가이드 §패턴별 예제의 번호와 1:1 대응한다.

| 파일 | 내용 |
|---|---|
| `e1-basic.yaml` | 가장 단순한 요청 — `keti.re.kr/lsu` 만. 나머지 전부 자동 |
| `e2-annotations.yaml` | workload-class·priority annotation |
| `e3-pair-3to1.yaml` | 두 작업 180:60 — 시간 3:1 분할 |
| `e4-colocation.yaml` | compute+memory 공동 배치 — 지켜줄 쪽 자동 완화 |
| `e5-priority.yaml` | high/low — high 의 선차용 |
| `e6-too-small.yaml` | (실패 예제) LSU 부족 → 메모리 할당 거부 |
| `e7-over-capacity.yaml` | (실패 예제) 노출량 초과 → Pending |
| `e8-npu.yaml` | NPU PE 블록 요청 |
| `e9-teardown.yaml` | 빠른 해제 시 파일 보존(atexit flush) |

```bash
kubectl create ns <내-네임스페이스>        # default 는 쓰지 말 것 (가이드 §하지 말 것)
kubectl apply -n <내-네임스페이스> -f e1-basic.yaml
tools/prism_check.sh e1-basic -n <내-네임스페이스>
```

이미지(`prism-infer:opt`)와 워크로드(파이썬 matmul 루프)는 **자리 표시**다 —
자기 이미지·명령으로 바꿔 쓰면 된다. PRISM 이 보는 것은 `resources.limits` 와
annotation 뿐이다.
