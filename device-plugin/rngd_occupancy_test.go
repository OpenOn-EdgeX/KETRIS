package main

import (
	"testing"
	"time"
)

// [Exp_122 4부] smi ps 파싱 — 실제 출력 형태로 검증.
func TestScanSMIPSParse(t *testing.T) {
	sample := `+---------+-------------+------+
| PID     | NPU core    | CMD  |
+---------+-------------+------+
| 1940701 | npu1:[0, 3] | /a/b |
+---------+-------------+------+
| 1941572 | npu1:[4, 7] | /a/c |
+---------+-------------+------+`
	occ := PEOccupancy{Busy: map[string]string{}}
	for _, line := range splitLines(sample) {
		m := smiPSLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		for pe := atoi(m[3]); pe <= atoi(m[4]); pe++ {
			occ.Busy[peKey(atoi(m[2]), pe)] = "smi_ps:pid=" + m[1]
		}
	}
	if len(occ.Busy) != 8 {
		t.Fatalf("PE 8개가 잡혀야 한다: %v", occ.Busy)
	}
	if occ.Busy["npu1/pe0"] != "smi_ps:pid=1940701" {
		t.Fatalf("pe0 귀속 오류: %v", occ.Busy["npu1/pe0"])
	}
	if occ.Busy["npu1/pe7"] != "smi_ps:pid=1941572" {
		t.Fatalf("pe7 귀속 오류: %v", occ.Busy["npu1/pe7"])
	}
}

// 단일 PE 도 npu1:[2, 2] 형태로 나온다.
func TestScanSMIPSSingle(t *testing.T) {
	m := smiPSLine.FindStringSubmatch(`| 42 | npu1:[2, 2] | x |`)
	if m == nil || m[3] != "2" || m[4] != "2" {
		t.Fatalf("단일 PE 파싱 실패: %v", m)
	}
}

func TestReservationsTTL(t *testing.T) {
	r := newReservations(50 * time.Millisecond)
	r.add([]string{"keti-rngd1-pe0", "keti-rngd1-pe1"})
	if len(r.active()) != 2 {
		t.Fatal("예약 직후 2개여야 한다")
	}
	time.Sleep(80 * time.Millisecond)
	if len(r.active()) != 0 {
		t.Fatal("TTL 경과 후 비어야 한다 — 기동 실패 파드의 예약이 새면 안 된다")
	}
}

// ★관측 실패 시 전량 Unhealthy 로 만들지 않는다(노드 NPU 통째 소실 방지).
func TestHealthOfSMIFailureKeepsHealthy(t *testing.T) {
	p := &RNGDPlugin{devices: map[string]RNGDDevice{
		"keti-rngd1-pe0": {NpuIdx: 1, PEIdx: 0},
		"keti-rngd1-pe1": {NpuIdx: 1, PEIdx: 1},
	}}
	occ := PEOccupancy{Busy: map[string]string{"npu1/pe0": "smi_ps:pid=1"},
		Err: errFake{}}
	h := p.healthOf(occ, map[string]bool{})
	for id, why := range h {
		if why != "" {
			t.Fatalf("smi 실패 시 %s 를 점유로 보면 안 된다: %q", id, why)
		}
	}
	// 그래도 우리 예약은 살아 있어야 한다
	h2 := p.healthOf(occ, map[string]bool{"keti-rngd1-pe1": true})
	if h2["keti-rngd1-pe1"] == "" {
		t.Fatal("smi 실패해도 우리 예약은 반영되어야 한다")
	}
}

func TestHealthOfBusyAndReserved(t *testing.T) {
	p := &RNGDPlugin{devices: map[string]RNGDDevice{
		"keti-rngd1-pe0": {NpuIdx: 1, PEIdx: 0},
		"keti-rngd1-pe4": {NpuIdx: 1, PEIdx: 4},
		"keti-rngd1-pe5": {NpuIdx: 1, PEIdx: 5},
	}}
	occ := PEOccupancy{Busy: map[string]string{"npu1/pe0": "smi_ps:pid=9"}}
	h := p.healthOf(occ, map[string]bool{"keti-rngd1-pe4": true})
	if h["keti-rngd1-pe0"] == "" {
		t.Fatal("외부 점유 PE 는 Unhealthy 여야 한다")
	}
	if h["keti-rngd1-pe4"] == "" {
		t.Fatal("우리 예약 PE 는 Unhealthy 여야 한다")
	}
	if h["keti-rngd1-pe5"] != "" {
		t.Fatal("점유·예약 없는 PE 는 Healthy 여야 한다")
	}
}

type errFake struct{}

func (errFake) Error() string { return "furiosa-smi ps 실패(테스트)" }

func splitLines(s string) []string {
	var out, cur = []string{}, ""
	for _, c := range s {
		if c == '\n' {
			out = append(out, cur)
			cur = ""
			continue
		}
		cur += string(c)
	}
	return append(out, cur)
}

func atoi(s string) int {
	n := 0
	for _, c := range s {
		n = n*10 + int(c-'0')
	}
	return n
}
