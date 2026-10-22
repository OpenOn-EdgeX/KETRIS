package main

// npu_test.go — NPU 모듈 유닛 테스트 (Exp_33 Phase 1).
// 가짜 devRoot 로 디스커버리·Allocate 응답 형식을 검증한다 (실장비 불필요).

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

// fakeWarboy lays out the Warboy device-node set observed on 232 (Exp_31b).
func fakeWarboy(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, n := range []string{
		"npu0", "npu0_mgmt", "npu0ch0", "npu0ch1", "npu0ch2", "npu0ch3",
		"npu0pe0", "npu0pe1", "npu0pe0-1",
	} {
		if err := os.WriteFile(filepath.Join(dir, n), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func newTestPlugin(t *testing.T, devRoot string) *NPUPlugin {
	t.Helper()
	pes, err := DiscoverNPUPEs(devRoot)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	p := &NPUPlugin{devRoot: devRoot, devices: map[string]NPUDevice{},
		stopCh: make(chan struct{})}
	for _, d := range pes {
		p.devices[npuDeviceID(d.NpuIdx, d.PEIdx)] = d
	}
	return p
}

func TestDiscoverPEs(t *testing.T) {
	dir := fakeWarboy(t)
	pes, err := DiscoverNPUPEs(dir)
	if err != nil {
		t.Fatal(err)
	}
	// pe0, pe1 만 — base npu0/fusion/mgmt/ch 는 광고 대상 아님
	if len(pes) != 2 {
		t.Fatalf("PE 2개 기대, got %d: %+v", len(pes), pes)
	}
	if pes[0].PEIdx != 0 || pes[1].PEIdx != 1 {
		t.Fatalf("pe0/pe1 기대: %+v", pes)
	}
}

func TestAllocateSinglePE(t *testing.T) {
	p := newTestPlugin(t, fakeWarboy(t))
	paths, envs, err := p.npuAllocateOne([]string{"keti-npu0-pe1"})
	if err != nil {
		t.Fatal(err)
	}
	// pe1 + mgmt + ch0..3 = 6 노드, fusion 아님
	if len(paths) != 6 {
		t.Fatalf("6개 노드 기대(pe+mgmt+ch4), got %d: %v", len(paths), paths)
	}
	if filepath.Base(paths[0]) != "npu0pe1" {
		t.Fatalf("첫 노드는 pe1: %v", paths)
	}
	if envs["KETI_NPU_FUSION"] != "0" || envs["KETI_NPU_PE"] != "npu0pe1" {
		t.Fatalf("env 불일치: %v", envs)
	}
	for _, want := range []string{"npu0_mgmt", "npu0ch0", "npu0ch3"} {
		found := false
		for _, p := range paths {
			if filepath.Base(p) == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("동반 노드 %s 누락: %v", want, paths)
		}
	}
}

func TestAllocateFusion(t *testing.T) {
	p := newTestPlugin(t, fakeWarboy(t))
	paths, envs, err := p.npuAllocateOne([]string{"keti-npu0-pe0", "keti-npu0-pe1"})
	if err != nil {
		t.Fatal(err)
	}
	// fusion 노드 1 + mgmt + ch0..3 = 6 (개별 pe 노드는 부여하지 않음)
	if filepath.Base(paths[0]) != "npu0pe0-1" {
		t.Fatalf("fusion 노드 기대: %v", paths)
	}
	if envs["KETI_NPU_FUSION"] != "1" || envs["KETI_NPU_PE"] != "npu0pe0-1" {
		t.Fatalf("fusion env 불일치: %v", envs)
	}
	for _, pth := range paths {
		if filepath.Base(pth) == "npu0pe0" || filepath.Base(pth) == "npu0pe1" {
			t.Fatalf("fusion 시 개별 PE 부여 금지 (all-or-nothing): %v", paths)
		}
	}
}

func TestAllocateErrors(t *testing.T) {
	p := newTestPlugin(t, fakeWarboy(t))
	if _, _, err := p.npuAllocateOne(nil); err == nil {
		t.Fatal("빈 요청은 오류여야 함")
	}
	if _, _, err := p.npuAllocateOne([]string{
		"keti-npu0-pe0", "keti-npu0-pe1", "keti-npu1-pe0"}); err == nil {
		t.Fatal("3개 요청은 오류여야 함")
	}
	if _, _, err := p.npuAllocateOne([]string{
		"keti-npu0-pe0", "keti-npu1-pe0"}); err == nil {
		t.Fatal("교차 NPU fusion 은 오류여야 함")
	}
}

func TestGetPreferredAllocationPair(t *testing.T) {
	p := newTestPlugin(t, fakeWarboy(t))
	resp, err := p.GetPreferredAllocation(context.Background(),
		&pluginapi.PreferredAllocationRequest{
			ContainerRequests: []*pluginapi.ContainerPreferredAllocationRequest{{
				AvailableDeviceIDs: []string{"keti-npu0-pe0", "keti-npu0-pe1"},
				AllocationSize:     2,
			}},
		})
	if err != nil {
		t.Fatal(err)
	}
	ids := resp.ContainerResponses[0].DeviceIDs
	if len(ids) != 2 {
		t.Fatalf("페어 기대: %v", ids)
	}
}

func TestAllocateGRPCShape(t *testing.T) {
	p := newTestPlugin(t, fakeWarboy(t))
	resp, err := p.Allocate(context.Background(), &pluginapi.AllocateRequest{
		ContainerRequests: []*pluginapi.ContainerAllocateRequest{
			{DevicesIds: []string{"keti-npu0-pe0"}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	car := resp.ContainerResponses[0]
	if len(car.Devices) != 6 {
		t.Fatalf("DeviceSpec 6개 기대: %d", len(car.Devices))
	}
	for _, d := range car.Devices {
		if d.Permissions != "rw" || d.HostPath != d.ContainerPath {
			t.Fatalf("DeviceSpec 형식 불일치: %+v", d)
		}
	}
}
