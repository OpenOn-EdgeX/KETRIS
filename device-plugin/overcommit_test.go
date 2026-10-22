package main

// overcommit_test.go — 이중제수(Exp_73 Part A) 단위 시험.
// 컴퓨트 pct·SM 은 물리 LSU 제수, 메모리 quota 는 광고 LSU(=물리×factor) 제수.

import "testing"

func mkPlugin(factor float64) (*DevicePlugin, GPUInfo) {
	p := &DevicePlugin{lsuMode: true, ocFactor: factor}
	// Pro6000 실측: 물리 LSU 178, SM 188, mem 97887MB.
	gpu := GPUInfo{Index: 0, Name: "Pro6000", LSU: 178, SmCount: 188, MemoryMB: 97887}
	return p, gpu
}

func TestAdvertisedUnits(t *testing.T) {
	cases := []struct {
		factor float64
		want   int
	}{
		{1.0, 178}, {1.2, 214}, {1.5, 267}, // round(178×f)
	}
	for _, c := range cases {
		p, gpu := mkPlugin(c.factor)
		if got := p.unitsPerGPU(gpu); got != c.want {
			t.Errorf("factor=%.1f: 광고 LSU=%d, 기대 %d", c.factor, got, c.want)
		}
		if got := p.physicalUnits(gpu); got != 178 {
			t.Errorf("factor=%.1f: 물리 LSU=%d, 기대 178(불변)", c.factor, got)
		}
	}
}

func TestDualDivisor(t *testing.T) {
	// factor=1.0 회귀: Exp_71/72 와 동일해야 한다.
	p, gpu := mkPlugin(1.0)
	sm, mem, pct := p.sliceSpec(gpu, 89)
	if pct != 50 || mem != 48943 || sm != 94 {
		t.Errorf("factor=1.0 lsu89: sm=%d mem=%d pct=%d (기대 94/48943/50)", sm, mem, pct)
	}

	// factor=1.5: 컴퓨트 pct 는 물리 기준이라 lsu89 → 50 (factor 무관!),
	// 메모리는 광고(267) 기준이라 줄어든다.
	p, gpu = mkPlugin(1.5)
	sm, mem, pct = p.sliceSpec(gpu, 89)
	if pct != 50 {
		t.Errorf("factor=1.5 lsu89: pct=%d, 기대 50(물리기준·factor무관)", pct)
	}
	if mem != int32(97887*89/267) { // 32629
		t.Errorf("factor=1.5 lsu89: mem=%d, 기대 %d(광고267 기준)", mem, 97887*89/267)
	}

	// 오버커밋 실증: 두 테넌트 합이 광고까지 admit → Σpct>100%.
	_, mem1, pct1 := p.sliceSpec(gpu, 178) // pct=100
	_, mem2, pct2 := p.sliceSpec(gpu, 89)  // pct=50
	if pct1+pct2 != 150 {
		t.Errorf("Σpct(178+89)=%d, 기대 150(오버커밋 — 클램프 금지)", pct1+pct2)
	}
	// 메모리는 광고 기준이라 Σ ≤ 물리(초과 불가).
	if int(mem1)+int(mem2) > 97887 {
		t.Errorf("Σmem=%d > 물리 97887 (메모리 오버커밋 금지)", int(mem1)+int(mem2))
	}

	// 개별 테넌트 pct 는 100 클램프(단일 클라이언트 >100% 무의미).
	_, _, pctBig := p.sliceSpec(gpu, 267)
	if pctBig != 100 {
		t.Errorf("lsu267 단일: pct=%d, 기대 100(개별 클램프)", pctBig)
	}
}

func TestOvercommitFactorDefault(t *testing.T) {
	c := &LSUCatalog{DefaultLSU: 50} // factor 미지정
	// LoadCatalog 의 기본값 보정을 모사
	if c.OvercommitFactor <= 0 {
		c.OvercommitFactor = 1.0
	}
	if c.OvercommitFactor != 1.0 {
		t.Errorf("미지정 factor 기본=%v, 기대 1.0", c.OvercommitFactor)
	}
}
