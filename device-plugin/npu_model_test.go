package main

// npu_model_test.go — Exp_47: PE 풀 분할·모델별 리소스·이중계상 방지.

import (
	"os"
	"path/filepath"
	"testing"
)

// 가짜 /dev: npu0pe0, npu0pe1 + mgmt/ch 세트.
func fakeDevRoot(t *testing.T) string {
	d := t.TempDir()
	for _, n := range []string{"npu0pe0", "npu0pe1", "npu0pe0-1", "npu0_mgmt",
		"npu0ch0", "npu0ch1", "npu0ch2", "npu0ch3"} {
		if err := os.WriteFile(filepath.Join(d, n), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return d
}

// 게이트 1(B): 프록시 PE 제외 — 직접 풀은 PE1 만 남는다.
func TestExcludeProxyPE(t *testing.T) {
	d := fakeDevRoot(t)
	p, err := NewNPUPlugin(d, "npu0pe0")
	if err != nil {
		t.Fatal(err)
	}
	if len(p.devices) != 1 {
		t.Fatalf("직접 풀 = 1개 기대, got %d", len(p.devices))
	}
	if _, ok := p.devices["keti-npu0-pe1"]; !ok {
		t.Fatalf("pe1 이 직접 풀에 있어야 함: %v", p.devices)
	}
	// ★이중계상 금지: 제외된 pe0 은 직접 풀 device 목록에 없어야 한다.
	if _, ok := p.devices["keti-npu0-pe0"]; ok {
		t.Fatal("프록시 PE 가 직접 풀에도 광고됨 — 이중계상")
	}
}

// 제외 없음(기존 동작 하위호환): PE 2개 광고.
func TestNoExcludeBackcompat(t *testing.T) {
	p, err := NewNPUPlugin(fakeDevRoot(t), "")
	if err != nil {
		t.Fatal(err)
	}
	if len(p.devices) != 2 {
		t.Fatalf("PE 2개 기대, got %d", len(p.devices))
	}
}

// 전 PE 제외는 오류 (빈 풀 방지).
func TestExcludeAllFails(t *testing.T) {
	d := t.TempDir()
	os.WriteFile(filepath.Join(d, "npu0pe0"), nil, 0o644)
	if _, err := NewNPUPlugin(d, "npu0pe0"); err == nil {
		t.Fatal("빈 직접 풀인데 오류가 없음")
	}
}

// 모델 리소스: 슬롯 광고 구성.
func TestModelSlots(t *testing.T) {
	p := NewNPUModelPlugin("resnet50", "npu0pe0", "/var/lib/kraken/npu")
	if p.resourceName != "keti.io/npu-resnet50" {
		t.Fatalf("리소스명: %s", p.resourceName)
	}
	if len(p.deviceIDs) != npuModelSlots {
		t.Fatalf("슬롯 %d개 기대, got %d", npuModelSlots, len(p.deviceIDs))
	}
}

// 모델 Allocate: 디바이스 노드 없음 + 프록시 소켓 마운트 + 몫 env.
func TestModelAllocate(t *testing.T) {
	p := NewNPUModelPlugin("resnet50", "npu0pe0", "/var/lib/kraken/npu")
	ids := []string{p.deviceIDs[6], p.deviceIDs[2], p.deviceIDs[0],
		p.deviceIDs[4], p.deviceIDs[1], p.deviceIDs[3], p.deviceIDs[5]}
	envs, mounts, err := p.modelAllocateOne(ids) // 7 슬롯
	if err != nil {
		t.Fatal(err)
	}
	if envs["KETI_NPU_PROXY_SLOTS"] != "7" || envs["KETI_NPU_PROXY_SHARE"] != "0.7" {
		t.Fatalf("몫 env: %v", envs)
	}
	if envs["KETI_NPU_PROXY_TENANT"] != p.deviceIDs[0] {
		t.Fatalf("canonical 테넌트는 정렬 최솟값: %v", envs["KETI_NPU_PROXY_TENANT"])
	}
	if envs["KETI_NPU_PROXY_DATA_SOCK"] != "/var/lib/kraken/npu/data.sock" {
		t.Fatalf("데이터 소켓 경로: %v", envs)
	}
	if len(mounts) != 1 || mounts[0].HostPath != "/var/lib/kraken/npu" {
		t.Fatalf("프록시 디렉토리 마운트: %v", mounts)
	}
}

// 슬롯 초과 요청 거부.
func TestModelAllocateBounds(t *testing.T) {
	p := NewNPUModelPlugin("resnet50", "npu0pe0", "/x")
	if _, _, err := p.modelAllocateOne(nil); err == nil {
		t.Fatal("빈 요청 허용됨")
	}
	over := make([]string, npuModelSlots+1)
	for i := range over {
		over[i] = "s"
	}
	if _, _, err := p.modelAllocateOne(over); err == nil {
		t.Fatal("슬롯 초과 허용됨")
	}
}
