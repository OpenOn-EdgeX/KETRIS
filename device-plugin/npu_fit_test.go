package main

import "testing"

func TestNPUFitExact(t *testing.T) {
	v := NPUFit(4, 4)
	if !v.OK || v.Waste != 0 || v.Actionable {
		t.Fatalf("정확 일치는 통과여야 한다: %+v", v)
	}
}

// tp=8 모델 22종이 8PE 를 받는 경로 — Exp_122 §7 의 "8PE 경로를 없앤다" 오류 방지.
func TestNPUFitTP8Gets8PE(t *testing.T) {
	v := NPUFit(8, 8)
	if !v.OK || v.Actionable {
		t.Fatalf("tp=8 모델의 8PE 요청은 정상이다: %+v", v)
	}
}

func TestNPUFitUnderRejects(t *testing.T) {
	for _, c := range [][2]int{{2, 4}, {4, 8}, {1, 4}} {
		v := NPUFit(c[0], c[1])
		if v.OK || !v.Actionable {
			t.Fatalf("PE %d < tp %d 는 거부여야 한다: %+v", c[0], c[1], v)
		}
	}
}

func TestNPUFitOverWarnsNotRejects(t *testing.T) {
	v := NPUFit(8, 4)
	if !v.OK {
		t.Fatal("초과는 실행이 되므로 거부하면 안 된다")
	}
	if v.Actionable {
		t.Fatal("초과는 경고여야 한다 — 거부하면 정상 워크로드를 막는다")
	}
	if v.Waste != 4 {
		t.Fatalf("노는 PE 4개여야 한다: %+v", v)
	}
}

// ★tp 미상이면 조용히 막지도, 조용히 통과시키지도 않는다(호출자가 경고).
func TestNPUFitUnknownTP(t *testing.T) {
	for _, tp := range []int{0, -1} {
		v := NPUFit(4, tp)
		if !v.OK || v.Actionable {
			t.Fatalf("tp 미상은 판정 보류여야 한다: %+v", v)
		}
	}
}
