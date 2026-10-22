package main

// npu_fit.go — [Exp_123 3-B] 요청 PE 수와 아티팩트 tp 의 적합 판정.
//
// Exp_122/123 실측이 만든 사실:
//   - 요청 PE < tp  → **실행 실패**. `Not enough devices... Required PEs: 4, Actual: 2`.
//     단 실패가 **5초 뒤**에 온다(모델 로드·컴파일 준비를 먼저 하고 device open 이 나중).
//     파드는 배치까지 되고 컨테이너 안에서 죽는다.
//   - 요청 PE > tp  → **실행은 된다. 자원을 논다.** tp=4 아티팩트에 8PE 를 주면
//     `furiosa-smi ps` 는 npu1:[0,7] 로 8개를 잡지만 계산은 4개만 쓴다.
//     8PE 단일이 4PE 단독 대비 +4.8% 뿐인 이유(Exp_122 3-B).
//   - 요청 PE = tp  → 정상.
//
// ★두 방향의 성격이 다르므로 대응도 다르다:
//     부족 = **거부**(어차피 죽는다. 5초 뒤 죽느니 즉시 거부가 낫다)
//     초과 = **경고**(도는 워크로드를 막지 않는다. 다만 낭비를 숫자로 알린다)
//   무조건 거부로 만들면 tp=8 모델 22종이 8PE 를 못 받는 사고가 난다(Exp_122 §7 오류).

import "fmt"

// NPUFitVerdict 는 적합 판정 결과다.
type NPUFitVerdict struct {
	OK        bool   // 실행 가능한가
	Waste     int    // 노는 PE 수 (초과 배정분)
	Reason    string // 사람이 읽을 사유
	Actionable bool  // 거부해야 하는가 (false = 경고만)
}

// NPUFit 는 요청 PE 수와 아티팩트 tp 로 적합을 판정한다.
//   tp <= 0 은 "모른다" — 판정하지 않는다(호출자가 T-5 경고).
func NPUFit(peCount, tp int) NPUFitVerdict {
	if tp <= 0 {
		return NPUFitVerdict{OK: true, Reason: "tp 미상 — 판정하지 않는다"}
	}
	switch {
	case peCount < tp:
		return NPUFitVerdict{
			OK: false, Actionable: true,
			Reason: fmt.Sprintf(
				"PE %d개 요청인데 아티팩트가 tp=%d 를 요구한다 — 컨테이너 안에서 "+
					"'Not enough devices' 로 죽는다(약 5초 뒤). 배치 전에 거부한다",
				peCount, tp),
		}
	case peCount > tp:
		return NPUFitVerdict{
			OK: true, Waste: peCount - tp,
			Reason: fmt.Sprintf(
				"PE %d개 요청인데 아티팩트는 tp=%d 만 쓴다 — %d개가 논다. "+
					"실행은 되지만 같은 카드를 tp 크기 블록 %d개로 쪼개면 처리량이 는다",
				peCount, tp, peCount-tp, peCount/tp),
		}
	default:
		return NPUFitVerdict{OK: true, Reason: "적합"}
	}
}
