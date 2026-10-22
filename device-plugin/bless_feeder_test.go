package main

// bless_feeder_test.go — 자동 배선 stale 배제·reconcile (Exp_73 Part B).

import (
	"net"
	"os"
	"path/filepath"
	"testing"
)

// mkSock creates a live unix DGRAM socket file at dir/<devID>/bless-1.sock.
func mkSock(t *testing.T, root, devID string) *net.UnixConn {
	t.Helper()
	d := filepath.Join(root, devID)
	if err := os.MkdirAll(d, 0o777); err != nil {
		t.Fatal(err)
	}
	sp := filepath.Join(d, "bless-1.sock")
	c, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: sp, Net: "unixgram"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestBlessWirerStaleExclusion(t *testing.T) {
	root := t.TempDir()
	// live: 할당됨 + 소켓 존재. stale: 소켓만 있고 할당 없음.
	c1 := mkSock(t, root, "keti-gpu0-lsu10")
	defer c1.Close()
	// stale 소켓 파일만 생성(리스너 닫아도 파일 잔존을 모사 — 그냥 파일로)
	sd := filepath.Join(root, "keti-gpu0-lsu99")
	os.MkdirAll(sd, 0o777)
	cStale := mkSock(t, root, "keti-gpu0-lsu99")
	cStale.Close() // 파일은 남고 리스너는 없음

	// lsu10 = 할당 89LSU, lsu99 = 할당 없음(stale)
	units := map[string]int{"keti-gpu0-lsu10": 89}
	w := newBlessFeederWirer(root, nil, func(id string) (int, bool) {
		u, ok := units[id]
		return u, ok
	}, 178)

	got := w.scan()
	s, ok := got["keti-gpu0-lsu10"]
	if !ok {
		t.Error("live 테넌트가 scan 에서 누락")
	} else if s.units != 89 {
		t.Errorf("ratio 용 units=%d, 기대 89(LSU 비례)", s.units)
	}
	if _, ok := got["keti-gpu0-lsu99"]; ok {
		t.Error("stale(할당없음) 소켓이 등록 대상에 포함 — units 게이트 실패")
	}
}

func TestRelaxedDecision(t *testing.T) {
	cases := []struct {
		name    string
		classes map[string]string
		want    map[string]bool // devID → arm 기대
	}{
		{"이종(compute+memory)→victim 해제",
			map[string]string{"c": "compute", "m": "memory"},
			map[string]bool{"c": true, "m": false}}, // memory 해제
		{"동종 compute→전원 arm(strict)",
			map[string]string{"c1": "compute", "c2": "compute"},
			map[string]bool{"c1": true, "c2": true}},
		{"★동종 memory→전원 arm(A-0: 양쪽 해제 금지=몫 소멸)",
			map[string]string{"m1": "memory", "m2": "memory"},
			map[string]bool{"m1": true, "m2": true}},
		{"미선언 혼재→전원 arm(안전 기본값)",
			map[string]string{"x": "", "m": "memory"},
			map[string]bool{"x": true, "m": true}}, // aggressor 없음 → strict
		{"이종 다수(2c+1m)→memory만 해제",
			map[string]string{"c1": "compute", "c2": "compute", "m": "memory"},
			map[string]bool{"c1": true, "c2": true, "m": false}},
	}
	for _, c := range cases {
		got := relaxedDecision(c.classes)
		for id, w := range c.want {
			if got[id] != w {
				t.Errorf("%s: %s arm=%v, 기대 %v", c.name, id, got[id], w)
			}
		}
	}
}

func TestReadClass(t *testing.T) {
	d := t.TempDir()
	os.WriteFile(filepath.Join(d, "class"), []byte("memory\n"), 0o644)
	if readClass(d) != "memory" {
		t.Error("memory 클래스 읽기 실패")
	}
	os.WriteFile(filepath.Join(d, "class"), []byte("garbage"), 0o644)
	if readClass(d) != "" {
		t.Error("무효 클래스는 \"\"(strict) 이어야")
	}
	if readClass(t.TempDir()) != "" {
		t.Error("파일 없으면 \"\"(미선언)")
	}
}

func TestRatioOf(t *testing.T) {
	w := newBlessFeederWirer("", nil, nil, 178)
	// strict 회귀: lsu89 → 0.5 (둘이면 Σ=1.0 → denom 1.0 → 각 duty 0.5 = 구 동작 동일)
	if got := w.ratioOf(89); got < 0.499 || got > 0.501 {
		t.Errorf("ratioOf(89)=%v, 기대 ~0.5", got)
	}
	// relaxed 단일: aggressor 0.5 → denom 1.0 하한 → 스로틀 0.5 (구: raw 89 → full)
	if got := w.ratioOf(178); got != 1.0 {
		t.Errorf("ratioOf(178)=%v, 기대 1.0", got)
	}
	// physLSU 0 = 구 동작(raw)
	w0 := newBlessFeederWirer("", nil, nil, 0)
	if got := w0.ratioOf(89); got != 89.0 {
		t.Errorf("physLSU=0 ratioOf(89)=%v, 기대 89(구 동작)", got)
	}
}
