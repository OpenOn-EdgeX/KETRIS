#!/usr/bin/env python3
"""Test 18: 페어링 예측 (Exp_49) — GPU 불필요.

pair_predict 규칙·가드·decide_pair 부착·★기존 동작 무변경(지표 부재=v1 폴백) 검증.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

from ratio.pair import decide_pair
from ratio.pair_predict import (ANCHOR_HETERO, ANCHOR_HOMO_C, ANCHOR_HOMO_M,
                                ANCHOR_CONTENDED_FLOOR, WALL_FILL_HI,
                                predict_pair)
from ratio.engine import CLASS_COMPUTE, CLASS_MEMORY

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def check(name, cond, info=""):
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  ({info})" if info else ""))
    results.append(cond)
    return cond


def req(name, cls, fill=None, r=0.5, conf="HIGH", density=None, cm=None):
    d = {"name": name, "r": r, "workload_class": cls, "confidence": conf}
    if fill is not None:
        d["device_fill"] = fill
    if density is not None:
        d["kernel_density"] = density
    if cm is not None:
        d["cm_ratio"] = cm
    return d


print("== 하위호환: 지표 부재 → available=False + 결정 무변경 ==")
base = decide_pair([req("A", CLASS_COMPUTE), req("B", CLASS_MEMORY)])
check("feasible 유지 + 예측 비활성", base["feasible"]
      and base["pairing_prediction"]["available"] is False)
withf = decide_pair([req("A", CLASS_COMPUTE, 0.28), req("B", CLASS_MEMORY, 0.16)])
same = all(base["members"][n][k] == withf["members"][n][k]
           for n in ("A", "B") for k in ("space_ratio", "time_ratio", "gate"))
check("★예측은 s/t/gate 결정에 무관여 (fill 유무 동일 결정)", same)

print("== 규칙: 이종 곱(wall_fill) 규칙 (Exp_56 개정) ==")
# 실측 케이스 봉인 — Exp_56 8건 + Exp_58 4건(resnet18/34 페어 1.121/1.116,
# convnext_small/base 는 CM 가드 케이스라 아래 별도 검증). cm 은 실측값 동봉
# (가드가 정답 케이스를 뒤집지 않음을 함께 봉인).
CASES = [("gpt2m", 0.163, 0.096, 0.380, "hetero_complementary"),
         ("regnet_y_400mf", 0.524, 0.344, 0.309, "hetero_complementary"),
         ("mobilenet", 0.680, 0.338, 0.414, "hetero_complementary"),
         ("densenet121", 0.695, 0.509, 0.266, "hetero_complementary"),
         ("decode_b1", 0.471, 0.845, 0.025, "hetero_complementary"),
         ("resnet50_train", 0.642, 0.646, 0.337, "hetero_space_contended"),
         ("wide_resnet50_2", 0.601, 0.931, 0.437, "hetero_space_contended"),
         ("vgg16", 0.615, 0.964, 0.443, "hetero_space_contended"),
         ("resnet18", 0.651, 0.876, 0.349, "hetero_space_contended"),
         ("resnet34", 0.599, 0.875, 0.406, "hetero_space_contended"),
         # Exp_60 — CM 창 경계 밖 실측 (창 상단/하단의 반대편 확인)
         ("decode_b2", 0.2195, 0.9324, 0.7269, "hetero_complementary"),
         ("mnasnet1_0", 0.664, 0.443, 0.4149, "hetero_complementary")]
ok = True
for name, f, d, cm, want in CASES:
    p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                      req("B", CLASS_MEMORY, f, density=d, cm=cm)])
    if p["category"] != want:
        ok = False
        print(f"      {name}: {p['category']} (기대 {want})")
check("실측 12케이스 카테고리 재현 (Exp_56/58 + Exp_60 창 경계 밖)", ok)

# ★알려진 한계 봉인 (Exp_60): roberta-base 는 cm 0.792 로 CM 창 밖이고
# wall_fill 0.273 로 낮아 규칙은 상보를 예측하지만 실측 OC 1.097(경계 하단,
# 사실상 경합)이다. 창을 이 케이스까지 넓히면 decode_b2(cm 0.727, 실측 1.198
# 상보)를 오판하므로 **cm 축 확장으로는 해결 불가**임이 반례로 증명됐다.
# 원인은 자기 유지율(ret50 0.668) — 규칙이 담지 못하는 차원. 규칙이 바뀌면
# 이 테스트가 실패해 재검토를 강제한다(한계의 명시적 봉인).
p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                  req("B", CLASS_MEMORY, 0.312, density=0.875, cm=0.7925)])
check("★알려진 예외 roberta: 규칙은 상보 예측 (실측 1.097 — Exp_60 §2 한계)",
      p["category"] == "hetero_complementary")

print("== CM MIXED 경계 보수 가드 (Exp_58) ==")
# convnext 계열 3건 — wall_fill<임계 + cm 창 안 → 보수 경합 (실측 전부 경합)
GUARD = [("convnext_tiny", 0.584, 0.627, 0.4913),
         ("convnext_small", 0.545, 0.626, 0.5415),
         ("convnext_base", 0.499, 0.641, 0.5971)]
ok = True
for name, f, d, cm in GUARD:
    p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                      req("B", CLASS_MEMORY, f, density=d, cm=cm)])
    if p["category"] != "hetero_space_contended" or "MIXED 경계" not in p["basis"]:
        ok = False
        print(f"      {name}: {p['category']}")
check("convnext 계열 3건 → 보수 경합 (실측 1.079~1.085)", ok)
p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                  req("B", CLASS_MEMORY, 0.524, density=0.344)])
check("cm 부재 → 가드 미발동 (기존 동작 무변경)",
      p["category"] == "hetero_complementary")
p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                  req("B", CLASS_MEMORY, 0.524, density=0.344, cm="bad")])
check("cm 형식 오류 → 가드 무시 (관용)", p["category"] == "hetero_complementary")
p = predict_pair([req("A", CLASS_COMPUTE, 0.28), req("B", CLASS_MEMORY, 0.16, density=0.10)])
check("wall_fill 낮음 → complementary/이종 앵커",
      p["category"] == "hetero_complementary" and p["expected_oc"] == ANCHOR_HETERO)
p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                  req("B", CLASS_MEMORY, 0.68, density=0.34)])
check("★fill 높아도 density 낮으면 complementary (mobilenet 사분면)",
      p["category"] == "hetero_complementary", p["basis"][:44])
p = predict_pair([req("A", CLASS_COMPUTE, 0.28),
                  req("B", CLASS_MEMORY, 0.7, density=0.7)], self_pair_oc=1.083)
check("wall_fill 높음 + self_pair_oc → contended/실측 앵커",
      p["category"] == "hetero_space_contended" and abs(p["expected_oc"] - 1.083) < 1e-9)
p = predict_pair([req("A", CLASS_COMPUTE, 0.28), req("B", CLASS_MEMORY, 0.7, density=0.7)])
check("contended 무앵커 → 보수 하한", p["expected_oc"] == ANCHOR_CONTENDED_FLOOR)
p = predict_pair([req("A", CLASS_COMPUTE, 0.28), req("B", CLASS_MEMORY, 0.5)])
check("density 부재 → 보수 취급(상한 가정 → contended)",
      p["category"] == "hetero_space_contended" and "보수 취급" in p["basis"])

print("== 규칙: 동종 (fill 불요) ==")
p = predict_pair([req("A", CLASS_COMPUTE), req("B", CLASS_COMPUTE)])
check("동종 C → 0.945", p["available"] and p["expected_oc"] == ANCHOR_HOMO_C)
p = predict_pair([req("A", CLASS_MEMORY), req("B", CLASS_MEMORY)])
check("동종 M → 1.152", p["available"] and p["expected_oc"] == ANCHOR_HOMO_M)

print("== 가드 ==")
p = predict_pair([req("A", CLASS_COMPUTE)])
check("1-tenant → 보류", not p["available"])
p = predict_pair([req("A", CLASS_COMPUTE, 0.3),
                  req("B", CLASS_MEMORY, 0.2, conf="LOW")])
check("confidence LOW → 보류 (Exp_37 가드)", not p["available"])
p = predict_pair([req("A", CLASS_COMPUTE, 0.3), req("B", CLASS_MEMORY, 1.7)])
check("fill 범위 밖 → 보류", not p["available"])
p = predict_pair([req("A", "UNCERTAIN", 0.3), req("B", CLASS_MEMORY, 0.2)])
check("UNCERTAIN 조합 → 보류", not p["available"])

n, p_ = len(results), sum(results)
print(f"\n결과: {p_}/{n}")
sys.exit(0 if p_ == n else 1)
