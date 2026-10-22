# KRAKEN LSU 명세 v1.0 — 정본 (Exp_46)

**정본 지위**: LSU(Logical Slice Unit) 정의·환산·노출 시맨틱의 단일 정본.
카탈로그 데이터 정본은 `lsu/catalog.json`(v1) — 본 문서는 그 해석 규칙이다.
모든 서술은 실측 근거 각주를 갖는다 (Exp_30 출력 규약 승계).

**상태**: `provisional` — 기관 기술회의 확정 전. 값·단위는 확정 시 개정될 수 있으며,
카탈로그 `status` 필드와 API 응답의 `provisional` 플래그가 이 상태를 노출한다.[^1]

관련 정본: 시간 축 분배는 `docs/semantics/time_sharing.md`, NPU 게이트는
`docs/semantics/npu_gate.md`, REST 표면은 `docs/api/controller_api.yaml`,
기관 간 인터페이스는 `docs/api/kw_interface_draft.md`.

---

## §1. 정의 — 앵커 워크로드 기준 처리율 환산

**1 LSU = 앵커 워크로드(resnet50_inference_fp16) 42.16 img/s** 에 해당하는 장치
처리 능력. 장치의 LSU 값 = `장치 앵커 처리율(img/s) ÷ 42.16` (반올림 정수).[^2]

- 앵커 단위의 출처: **NVIDIA RTX A6000 = 65 LSU := 2740.7 img/s 실측** (calibration
  기준점, 2026-06-18).[^2]
- LSU 는 **용량(capacity) 단위**이며 시간 배분(t)·공간 배분(s)과 독립이다 —
  "몇 LSU 를 할당받았는가"가 계약, s×t 분해는 노드 내 집행 문제다
  (time_sharing.md §5, kw 인터페이스 A-1 안건).[^3]

## §2. 카탈로그 v1 값 (2026-07-28 현재)

| 장치 | LSU | 실측 | 비고 |
|---|---|---|---|
| NVIDIA RTX A6000 | 65 | ✓ | calibration 기준점 (2740.7 img/s) |
| NVIDIA RTX PRO 6000 Blackwell SE | 178 | ✓ | 7513.5 img/s @232, 3회 편차 ~1.5%[^4] |
| NVIDIA A100 | 100 | ✗ | PROJECT_CONTEXT 잠정 예시값 |
| Furiosa Warboy | **17 / PE** | ✓ | int8, 단일 PE 699.2 img/s ÷ 42.16 (Exp_37)[^5] |

- **NPU 는 PE 당 값**이다 (`unit: per_PE`): PE 가 배타 할당 단위이므로
  (npu_gate.md §1) K8s 노출 단위(`keti.io/npu` 1개 = PE 1개)와 정렬된다.
  Warboy 1장(2 PE) 총량 = 34 LSU 가 아니라 "17 LSU PE × 2" 로 읽는다.
- **fusion(2PE 통합) ≠ ×2**: fusion 처리율은 단일 PE 의 ×1.38 (964 vs 699 img/s) —
  지연 최적화 모드이며 LSU 산정에는 PE 단위 값을 쓴다.[^6]
- **정밀도 플래그**: GPU 앵커=fp16, Warboy=int8 전용. 정밀도 상이는 flags 로
  구분하고 억지 통일하지 않는다 (GPU int8 실측 불가 — TensorRT 부재).[^7]

## §2-1. 물리 LSU vs 광고(논리) LSU — 오버커밋 2단 의미 (Exp_73)

**★외부 협의(고려대 SLA 등)에서 "LSU N"이 물리 보장 용량으로 오해되지 않도록 용어를 분리한다.**

- **물리 LSU** = §1 정의(측정값). 1 LSU = 42.16 img/s 의 실제 처리 능력. Pro6000 = 178.
- **광고(논리) LSU** = 물리 LSU × `overcommit_factor`(catalog 설정). K8s 에 광고·admit
  되는 예약 단위다. factor>1 이면 물리보다 많이 광고 = **오버커밋**. Pro6000 factor=1.5 → 광고 267.
- **집행 제수 분리**(Exp_72 §4 / Exp_73 Part A):
  - 컴퓨트(MPS pct·virtual SM) 제수 = **물리 LSU** → ΣreqUnits 가 물리를 넘으면 Σpct>100%
    (오버커밋의 실체 — 초과분은 시간 게이트가 흡수, Exp_73 D-1). **Σpct>100% 는 정상, 클램프 금지.**
  - 메모리 quota 제수 = **광고 LSU** → Σ(모든 테넌트) ≤ 물리 메모리 항상 성립(메모리는 오버커밋 불가).
- **factor 기본값 = 1.0**(오버커밋 없음, 물리=광고). >1 값은 워크로드 조합의 OC 효율
  실측으로만 확정한다 — **동종 compute-bound 는 OC 효율 ≤1(이득 없음/손해), 이득은
  이종·버스트(유휴 갭)에서만**(Exp_73 D-2, Exp_50/52 계보). 230 A6000 의 1.5 를 이월하지 않는다.
- **용어 규약**: 대외 문서·API 에서 용량 계약을 말할 때는 "물리 LSU"를, 스케줄러 admit
  단위를 말할 때는 "광고 LSU(factor 포함)"를 명시한다. 그냥 "LSU N"은 금지(오해 유발).
- 상태: `overcommit_factor` 는 `provisional` — 기관 협의 및 이종 OC 실측 전 1.0 고정.

## §3. 노출·배분 체계 (Exp_31~33 실장)

```
lsu/catalog.json ── device-plugin(--lsu) 모델명 exact match
    → K8s Extended Resource keti.re.kr/lsu (Pro6000: 178개 광고)
    → 파드 requests (예: 89 LSU)
    → Allocate: units 비례 배분 — sm = 188×89/178 = 94, mem = 97887×89/178 MB,
      MPS pct = 50 (BLESS_LIMIT_PCT 주입, MPS 하에서만 실효 — O-3)
```

- 비례 배분 검증: 13 LSU → SM 13/188 비례·mem 7149MB·MPS 7% (dry-run exact match).[^8]
- 라이브 배치: 232 DaemonSet 이 광고·주입 담당 (Exp_42 — `kraken-device-plugin:exp42`).
- 내부 LSU 배분 독립성: 내부 회계(charged 비율)는 LSU 비율과 오차 ≤0.0064 로 정합
  (외부 처리율은 이종 페어에서 prefill 편향 — O-1, 정의 차이).[^9]

## §4. 2축 확장(v2) — ★영구 미채택 (Exp_61 종결 판정)

Exp_37 실측으로 **축간 비 상이**가 확정됐다: A6000→Pro6000 이 연산축 ×2.74
(resnet50) vs 대역폭축 ×2.00 (opt-6.7b decode) — 스칼라 1축이 두 자원을 동시에
대표하지 못한다.[^10] 이에 2축(compute_lsu/bandwidth_lsu)+flags 의
`catalog_v2_draft.json` 이 작성됐고, 채택 조건은 "워크로드 축 선택 훅이 커널명
분류를 넘어 cm_ratio 연속값+grid/shape 보정으로 보강되어 재검증될 것"이었다.[^11]

**Exp_61 종결 판정: 그 조건은 현 설계 제약(무침습 인터포지션 + 온라인) 하에서
충족 불가 — v2 draft 는 영구 미채택으로 종결한다.**[^12] 근거 3경로 전부 막힘:

1. **launch 파라미터 경로(원 조건) — 구조적 불가**: 축 선택에 필요한 것은 산술
   강도(문제 형상)인데 **형상(M=1)이 launch 파라미터에 존재하지 않는다** —
   llama b=1 GEMV 와 진짜 GEMM 이 같은 시그니처 공간(occ·smem·blocks)에 겹침.
   fill 규칙 재실행도 24.7% 로 v1 20.1%보다 나쁨(스칼라 14.8% 유지) — Exp_48
   "축 선택 무효 확정".
2. **CUPTI Metrics 경로(launch 밖 정보) — 온라인 부적합**: dram_throughput 은
   커널 replay 방식이라 온라인 사용 불가, Exp_58 불채택 확정.
3. **ret50 축(Exp_60) — 전용 불가**: ret50 은 페어 **공간 경합(OC) 예측** 지표로,
   연산/대역폭 **축 판별과는 직교**한다(같은 cm 대에서 ret50 이 갈림 — decode b2
   0.881 vs roberta 0.668). 또한 단독 실측 2회가 필요한 비용 구조라, 그 비용을
   지불한다면 양축 직접 실측이 가능해져 "훅"의 존재 이유가 소멸한다.

조건 재정의(iii)도 기각: 남는 길은 프레임워크 레벨 형상 힌트뿐인데 앱 협조가
필요해 무침습 원칙 밖이고, 초기 배치 추정 오차(±15%)는 폐루프(Exp_26 feeder)가
이미 흡수한다 — 축 선택 훅의 실용 필요성 자체가 낮다.[^12]

**확정 대안 — v1 스칼라 + 축 스프레드 공시**: K8s 노출·계약은 v1 스칼라(연산축,
Pro6000=178 불변)를 쓰고, **축간 비 상이(연산 ×2.74 vs 대역폭 ×2.00 — 대역폭형
워크로드에서 스칼라가 최대 +37% 과대)를 소비자 문서에 공시**한다(본 절이 공시
정본). 실측 벡터값(Pro6000 bandwidth_lsu=130, Warboy=null)은 draft 파일에 참고
데이터로 보존하며 계약에 쓰지 않는다.

---

[^1]: lsu/catalog.json `status`; kw_interface_draft.md §2-2 (provisional 플래그).
[^2]: lsu/catalog.json `unit_img_per_s`/`unit_source`; lsu/benchmark.py (측정 절차).
[^3]: docs/api/kw_interface_draft.md §1·회의 안건 A-1 (LSU 사영 규칙 미합의).
[^4]: Exp_31 사전 실측 (reports/LSU_EXPOSURE_REPORT.md; 2026-07-16 @232, 7422/7534/7514 img/s).
[^5]: Exp_37 §NPU (reports/Exp_37_lsu_catalog_v2/report.md — 699.2 img/s int8 단일 PE); Exp_43 재확인 708.4/695.1 img/s (reports/Exp_43_npu_inference/report.md §Phase 1 — 17 값 유지 범위).
[^6]: Exp_37 fusion ×1.38; Exp_43 재현 ×1.40 (964 vs 1349 합산 비교는 PE×2 기준 — fusion 964 vs 단일 699 = 1.38).
[^7]: Exp_37 §cross_device_precision (catalog_v2_draft.json anchors 주석 — 옵션 b 채택).
[^8]: Exp_3/LSU_EXPOSURE_REPORT dry-run; Exp_33 §E2E (89 LSU → sm94/mem48943/mps50 — reports/Exp_33_e2e_expose/report.md).
[^9]: Exp_38(재사용) LSU 배분 독립성 (reports/Exp_38_lsu_share_independence/report.md).
[^10]: Exp_37 §축간 비 (reports/Exp_37_lsu_catalog_v2/report.md — 2.74 vs 2.00; STREAM triad 2.17 상관).
[^11]: Exp_37 §5 (report.md — llama b=1 오분류); catalog_v2_draft.json `axis_selection_hint`.
[^12]: **Exp_61 Phase 3 종결 판정** (reports/Exp_61_space_mode_capped_catalog/report.md §3 — 실측 아님, 조건 재평가). 근거: Exp_48 §3-2 (reports/Exp_48_class_v2/report.md — "v2 도 축 선택엔 불충분", launch 파라미터에 문제 형상 없음 반증 ②); Exp_58 §1-5 (CUPTI Metrics 불채택 — replay=온라인 부적합); Exp_60 §2-3 (ret50 = 페어 OC 예측 지표, 채택 보류·비용 구조 상이); Exp_37 §7 (폐루프 흡수 메모 — 축 모델은 초기 배치 추정용인데 ±15% 는 feeder 수렴으로 흡수).
