package main

// rngd_test.go — RNGD PE 플러그인 단위 테스트 (Exp_70).
// 실기기 /dev/rngd 레이아웃(Exp_64 실측)을 임시 디렉토리로 재현한다.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRNGDRoot creates the 36-node layout of /dev/rngd (npu1).
func fakeRNGDRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	names := []string{"npu1mgmt", "npu1bar0", "npu1bar2", "npu1bar4",
		"npu1dmar", "npu1p2pmem"}
	for pe := 0; pe < 8; pe++ {
		names = append(names, fmt.Sprintf("npu1pe%d", pe))
	}
	// fusion 노드: 정렬 페어 4 + 쿼드 2 (pe0-7 은 실기기에 없음 — Exp_67)
	names = append(names, "npu1pe0-1", "npu1pe2-3", "npu1pe4-5", "npu1pe6-7",
		"npu1pe0-3", "npu1pe4-7")
	for ch := 0; ch < 8; ch++ {
		names = append(names, fmt.Sprintf("npu1ch%d", ch),
			fmt.Sprintf("npu1ch%dr", ch))
	}
	for _, n := range names {
		if err := os.WriteFile(filepath.Join(root, n), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestDiscoverRNGDPEs(t *testing.T) {
	root := fakeRNGDRoot(t)
	pes, err := DiscoverRNGDPEs(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(pes) != 8 {
		t.Fatalf("PE 8개 기대, %d개", len(pes))
	}
}

func TestRNGDValidateBlock(t *testing.T) {
	id := func(pe int) string { return rngdDeviceID(1, pe) }
	cases := []struct {
		ids []string
		ok  bool
	}{
		{[]string{id(3)}, true},                             // 1PE 임의 위치
		{[]string{id(0), id(1)}, true},                      // 정렬 페어
		{[]string{id(2), id(3)}, true},
		{[]string{id(1), id(2)}, false},                     // 비정렬 페어
		{[]string{id(4), id(5), id(6), id(7)}, true},        // 정렬 쿼드
		{[]string{id(2), id(3), id(4), id(5)}, false},       // 비정렬 쿼드
		{[]string{id(0), id(1), id(2)}, false},              // 수량 3 불가
		{[]string{id(0), id(1), id(2), id(3), id(4), id(5), id(6), id(7)}, true},
		{[]string{id(0), id(0)}, false},                     // 중복
	}
	for _, c := range cases {
		_, _, _, err := rngdValidateBlock(c.ids)
		if (err == nil) != c.ok {
			t.Errorf("ids=%v: ok 기대=%v, err=%v", c.ids, c.ok, err)
		}
	}
}

func TestRNGDPreferredBlock(t *testing.T) {
	all := make([]string, 0, 8)
	for pe := 0; pe < 8; pe++ {
		all = append(all, rngdDeviceID(1, pe))
	}
	// 전체 가용에서 4PE → 0-3 블록
	ids, err := rngdPreferredBlock(all, 4)
	if err != nil || len(ids) != 4 || ids[0] != rngdDeviceID(1, 0) {
		t.Fatalf("전체 가용 4PE: %v %v", ids, err)
	}
	// 0-3 소진 상태에서 4PE → 4-7 블록
	ids, err = rngdPreferredBlock(all[4:], 4)
	if err != nil || len(ids) != 4 || ids[0] != rngdDeviceID(1, 4) {
		t.Fatalf("부분 가용 4PE: %v %v", ids, err)
	}
	// 비정렬 가용(1,2)만 남은 상태에서 2PE → 정렬 블록 불가
	if _, err = rngdPreferredBlock([]string{rngdDeviceID(1, 1), rngdDeviceID(1, 2)}, 2); err == nil {
		t.Fatal("비정렬 잔여에서 2PE 성공하면 안 됨")
	}
	// 수량 3 → 오류
	if _, err = rngdPreferredBlock(all, 3); err == nil {
		t.Fatal("수량 3 성공하면 안 됨")
	}
}

func TestRNGDAllocateOne(t *testing.T) {
	root := fakeRNGDRoot(t)
	p := &RNGDPlugin{devRoot: root, devices: map[string]RNGDDevice{}}

	// 4PE 블록 0-3: PE 4 + fusion(0-1,2-3,0-3) 3 + BASE 6 + ch 16 = 29 노드
	paths, envs, err := p.rngdAllocateOne([]string{
		rngdDeviceID(1, 0), rngdDeviceID(1, 1), rngdDeviceID(1, 2), rngdDeviceID(1, 3)})
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) != 29 {
		t.Fatalf("경로 29개 기대, %d개: %v", len(paths), paths)
	}
	if envs["KETI_NPU_DEVICES"] != "npu:1:0-3" {
		t.Fatalf("KETI_NPU_DEVICES=%s", envs["KETI_NPU_DEVICES"])
	}
	// 블록 밖 PE 노드가 부여 세트에 없어야 격리 성립
	for _, pth := range paths {
		for pe := 4; pe < 8; pe++ {
			if strings.HasSuffix(pth, fmt.Sprintf("npu1pe%d", pe)) {
				t.Fatalf("블록 밖 PE 유출: %s", pth)
			}
		}
	}

	// 1PE: PE 1 + BASE 6 + ch 16 = 23 노드, fusion 없음
	paths, envs, err = p.rngdAllocateOne([]string{rngdDeviceID(1, 5)})
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) != 23 || envs["KETI_NPU_DEVICES"] != "npu:1:5" {
		t.Fatalf("1PE: %d개 %s", len(paths), envs["KETI_NPU_DEVICES"])
	}

	// 8PE: PE 8 + fusion 6(존재분 — pe0-7 없음) + BASE 6 + ch 16 = 36 노드
	all := make([]string, 0, 8)
	for pe := 0; pe < 8; pe++ {
		all = append(all, rngdDeviceID(1, pe))
	}
	paths, envs, err = p.rngdAllocateOne(all)
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) != 36 || envs["KETI_NPU_DEVICES"] != "npu:1:0-7" {
		t.Fatalf("8PE: %d개 %s", len(paths), envs["KETI_NPU_DEVICES"])
	}

	// 비정렬 → 오류
	if _, _, err = p.rngdAllocateOne([]string{
		rngdDeviceID(1, 1), rngdDeviceID(1, 2)}); err == nil {
		t.Fatal("비정렬 허용되면 안 됨")
	}
}
