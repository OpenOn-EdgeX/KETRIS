package main

import (
	"context"
	"encoding/json"
	api "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const migUUID = "MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf"

func fixture(t *testing.T) (*Plugin, Config) {
	t.Helper()
	dir, err := os.MkdirTemp("", "km-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	lib := filepath.Join(dir, "lib.so")
	os.WriteFile(lib, []byte("fixture"), 0600)
	cfg := Config{Node: "node", GPUUUID: "GPU-895722c8-b706-27ae-018d-c43e405e6c35", MIGUUID: migUUID, Generation: "g1", ResourceName: "keti.re.kr/mig-4bf488e0e54112ad-share", StateDir: dir, PipeDir: filepath.Join(dir, "mps"), LibPath: lib, CudaLibDir: "/cuda", Units: 200, Active: true}
	p, err := NewPlugin(cfg, "")
	if err != nil {
		t.Fatal(err)
	}
	return p, cfg
}

func TestReallocationWaitsForOwnerRetirement(t *testing.T) {
	p, cfg := fixture(t)
	a, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	old := a.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"]
	os.MkdirAll(filepath.Join(cfg.StateDir, "bindings"), 0700)
	os.WriteFile(filepath.Join(cfg.StateDir, "bindings", old+".json"), []byte(`{"pod_uid":"old-uid","phase":"bound"}`), 0600)
	go func() {
		time.Sleep(200 * time.Millisecond)
		os.MkdirAll(filepath.Join(cfg.StateDir, "tombstones"), 0700)
		os.WriteFile(filepath.Join(cfg.StateDir, "tombstones", old), []byte("done"), 0600)
	}()
	b, err := p.Allocate(context.Background(), request(p, 0, 25))
	if err != nil {
		t.Fatal(err)
	}
	if b.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"] == old {
		t.Fatal("retired Pod identity reused")
	}
}
func request(p *Plugin, start, count int) *api.AllocateRequest {
	var ids []string
	for i := start; i < start+count; i++ {
		ids = append(ids, p.DeviceID(i))
	}
	return &api.AllocateRequest{ContainerRequests: []*api.ContainerAllocateRequest{{DevicesIds: ids}}}
}
func TestAllocatePinsMIGAndIndependentSpaceLimit(t *testing.T) {
	p, _ := fixture(t)
	r, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	c := r.ContainerResponses[0]
	if c.Envs["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] != "50" || c.CdiDevices[0].Name != "nvidia.com/gpu="+migUUID {
		t.Fatalf("wrong device/cap: %+v", c)
	}
	if c.Envs["BLESS_TIME_START_GATED"] != "1" {
		t.Fatal("workload can start ungated")
	}
	if _, ok := c.Envs["BLESS_MEM_QUOTA_MB"]; ok {
		t.Fatal("unmeasured VRAM subquota must not be claimed")
	}
}
func TestAllocateRetryPreservesLeaseAndJournal(t *testing.T) {
	p, cfg := fixture(t)
	a, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	b, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	if a.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"] != b.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"] {
		t.Fatal("duplicate retry created a second lease")
	}
	files, _ := filepath.Glob(filepath.Join(cfg.StateDir, "allocations", "*.json"))
	if len(files) != 1 {
		t.Fatalf("duplicate journal: %d", len(files))
	}
	bytes, _ := os.ReadFile(files[0])
	var body map[string]any
	if json.Unmarshal(bytes, &body) != nil || body["phase"] != "allocated" || body["mig_uuid"] != migUUID {
		t.Fatal("incorrect allocated evidence")
	}
}
func TestAllocateRejectsOverlappingUnitsAndInvalidIDs(t *testing.T) {
	p, _ := fixture(t)
	if _, err := p.Allocate(context.Background(), request(p, 0, 50)); err != nil {
		t.Fatal(err)
	}
	for _, req := range []*api.AllocateRequest{request(p, 0, 25), request(p, 50, 101), {ContainerRequests: []*api.ContainerAllocateRequest{{DevicesIds: []string{"other-mig-u001"}}}}} {
		if _, err := p.Allocate(context.Background(), req); err == nil {
			t.Fatal("invalid share allocation accepted")
		}
	}
}
func TestMultiContainerResponsesDoNotShareControlDirectory(t *testing.T) {
	p, _ := fixture(t)
	req := request(p, 0, 50)
	req.ContainerRequests = append(req.ContainerRequests, request(p, 100, 50).ContainerRequests[0])
	r, err := p.Allocate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.ContainerResponses) != 2 || r.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"] == r.ContainerResponses[1].Envs["KETRIS_ALLOCATION_ID"] {
		t.Fatal("container identities collapsed")
	}
}
func TestMissingMIGGenerationFailsClosed(t *testing.T) {
	p, cfg := fixture(t)
	cfg.Generation = "g2"
	data, _ := json.Marshal(cfg)
	path := filepath.Join(cfg.StateDir, "config.json")
	os.WriteFile(path, data, 0600)
	p.ConfigPath = path
	if _, err := p.Allocate(context.Background(), request(p, 0, 50)); err == nil {
		t.Fatal("stale MIG generation accepted")
	}
}
func TestJournalFailureDoesNotGrantDevice(t *testing.T) {
	p, cfg := fixture(t)
	os.WriteFile(filepath.Join(cfg.StateDir, "allocations"), []byte("not-directory"), 0600)
	if _, err := p.Allocate(context.Background(), request(p, 0, 50)); err == nil {
		t.Fatal("device granted with missing allocation evidence")
	}
}
func TestCompletedLeaseCanBeReallocatedWithNewIdentity(t *testing.T) {
	p, cfg := fixture(t)
	a, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	old := a.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"]
	os.MkdirAll(filepath.Join(cfg.StateDir, "tombstones"), 0700)
	os.WriteFile(filepath.Join(cfg.StateDir, "tombstones", old), []byte("completed"), 0600)
	b, err := p.Allocate(context.Background(), request(p, 0, 50))
	if err != nil {
		t.Fatal(err)
	}
	if b.ContainerResponses[0].Envs["KETRIS_ALLOCATION_ID"] == old {
		t.Fatal("completed tenant resurrected")
	}
}

func TestCorruptRetryCannotEscalateMPSCap(t *testing.T) {
	p, cfg := fixture(t)
	if _, err := p.Allocate(context.Background(), request(p, 0, 50)); err != nil {
		t.Fatal(err)
	}
	files, _ := filepath.Glob(filepath.Join(cfg.StateDir, "allocations", "*.json"))
	raw, _ := os.ReadFile(files[0])
	var b Binding
	json.Unmarshal(raw, &b)
	b.SpacePercent = 100
	raw, _ = json.Marshal(b)
	os.WriteFile(files[0], raw, 0600)
	if _, err := p.Allocate(context.Background(), request(p, 0, 50)); err == nil {
		t.Fatal("corrupt retry changed requested 50% to 100%")
	}
}
