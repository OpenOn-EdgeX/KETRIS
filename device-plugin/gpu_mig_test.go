package main

import (
	"context"
	api "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
	"os"
	"path/filepath"
	"testing"
)

func fakeSMI(t *testing.T, body string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "nvidia-smi"), []byte("#!/bin/sh\n"+body+"\n"), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+":"+os.Getenv("PATH"))
}

func TestMIGEnabledGPUNotAdvertised(t *testing.T) {
	fakeSMI(t, `printf '%s\n' '0, RTX PRO 6000, 97887, GPU-895722c8-b706-27ae-018d-c43e405e6c35, Enabled'`)
	g, err := DiscoverGPUs()
	if err != nil {
		t.Fatal(err)
	}
	if len(g) != 0 {
		t.Fatalf("MIG parent advertised as full GPU: %v", g)
	}
	p := NewDevicePlugin("keti.re.kr/lsu", 1, true, nil, g)
	if len(p.buildListResponse().Devices) != 0 {
		t.Fatal("empty full GPU set must have no devices")
	}
}

func TestMIGDisabledGPUStillAdvertised(t *testing.T) {
	fakeSMI(t, `printf '%s\n' '0, RTX PRO 6000, 97887, GPU-895722c8-b706-27ae-018d-c43e405e6c35, Disabled'`)
	g, err := DiscoverGPUs()
	if err != nil {
		t.Fatal(err)
	}
	if len(g) != 1 || g[0].Index != 0 {
		t.Fatalf("non-MIG GPU lost: %v", g)
	}
}

func TestMIGModeUnknownFailsClosed(t *testing.T) {
	fakeSMI(t, `printf '%s\n' '0, RTX PRO 6000, 97887, GPU-895722c8-b706-27ae-018d-c43e405e6c35, mystery'`)
	if _, err := DiscoverGPUs(); err == nil {
		t.Fatal("unknown MIG mode must not advertise a full GPU")
	}
}

func TestMIGTransitionRejectsStaleFullGPUAllocate(t *testing.T) {
	fakeSMI(t, `printf '%s\n' '0, RTX PRO 6000, 97887, GPU-895722c8-b706-27ae-018d-c43e405e6c35, Disabled'`)
	g, err := DiscoverGPUs()
	if err != nil {
		t.Fatal(err)
	}
	g[0].LSU = 100
	p := NewDevicePlugin("keti.re.kr/lsu", 1, true, nil, g)
	fakeSMI(t, `printf '%s\n' '0, Enabled'`)
	_, err = p.Allocate(context.Background(), &api.AllocateRequest{ContainerRequests: []*api.ContainerAllocateRequest{{DevicesIds: []string{"keti-gpu0-lsu0"}}}})
	if err == nil {
		t.Fatal("stale full GPU allocation accepted after MIG transition")
	}
}
