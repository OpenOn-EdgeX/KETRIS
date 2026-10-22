package main

// npu_lifecycle_test.go — Exp_53: 자동 등록/해제/복원/watcher canonical.

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// mock controller — /feeder/* 호출 기록
type feederMock struct {
	mu    sync.Mutex
	calls []map[string]interface{}
}

func (f *feederMock) server() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			var body map[string]interface{}
			json.NewDecoder(r.Body).Decode(&body)
			body["_path"] = r.URL.Path
			f.mu.Lock()
			f.calls = append(f.calls, body)
			f.mu.Unlock()
			w.Write([]byte(`{"ok": true}`))
		}))
}

func (f *feederMock) paths() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, c := range f.calls {
		out = append(out, c["_path"].(string))
	}
	return out
}

func newTestModel(t *testing.T, ctrlURL string) (*NPUModelPlugin, chan string) {
	dir := t.TempDir()
	// admin 소켓 수신부 (프록시 모사)
	adminMsgs := make(chan string, 16)
	addr, _ := net.ResolveUnixAddr("unixgram", filepath.Join(dir, "admin.sock"))
	conn, err := net.ListenUnixgram("unixgram", addr)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		buf := make([]byte, 256)
		for {
			n, err := conn.Read(buf)
			if err != nil {
				return
			}
			adminMsgs <- string(buf[:n])
		}
	}()
	p := NewNPUModelPlugin("resnet50", "npu0pe0", dir)
	p.controller = NewControllerClient(ctrlURL)
	return p, adminMsgs
}

func TestAutoRegister(t *testing.T) {
	fm := &feederMock{}
	srv := fm.server()
	defer srv.Close()
	p, admin := newTestModel(t, srv.URL)

	p.autoRegister("keti-npu0pe0-resnet50-s00", 7)
	if got := <-admin; got != "register keti-npu0pe0-resnet50-s00" {
		t.Fatalf("admin register 미발신: %q", got)
	}
	ps := fm.paths()
	if len(ps) != 2 || ps[0] != "/feeder/register" || ps[1] != "/feeder/arm" {
		t.Fatalf("feeder 호출 순서: %v", ps)
	}
	if r := fm.calls[0]["time_ratio"].(float64); r != 0.7 {
		t.Fatalf("ratio = slots/10 기대 0.7, got %v", r)
	}
	if fm.calls[0]["resource"] != "npu" {
		t.Fatalf("resource 태그 npu 기대: %v", fm.calls[0]["resource"])
	}
	if !p.IsSlotRegistered("keti-npu0pe0-resnet50-s00") {
		t.Fatal("allocs 미기록")
	}
}

func TestCleanupPolicyA(t *testing.T) {
	fm := &feederMock{}
	srv := fm.server()
	defer srv.Close()
	p, admin := newTestModel(t, srv.URL)
	p.allocs["s00"] = 7
	p.allocs["s07"] = 3

	p.CleanupTenant("s07")
	if got := <-admin; got != "unregister s07" {
		t.Fatalf("admin unregister 미발신: %q", got)
	}
	ps := fm.paths()
	// 정책 A(기본): release + deregister 만 — ratios 재정규화 없음
	want := []string{"/feeder/release", "/feeder/deregister"}
	if len(ps) != 2 || ps[0] != want[0] || ps[1] != want[1] {
		t.Fatalf("정책 A 호출: %v (기대 %v)", ps, want)
	}
	if p.IsSlotRegistered("s07") || !p.IsSlotRegistered("s00") {
		t.Fatal("allocs 정리 오류")
	}
}

func TestCleanupPolicyB(t *testing.T) {
	fm := &feederMock{}
	srv := fm.server()
	defer srv.Close()
	p, _ := newTestModel(t, srv.URL)
	p.redistribute = true
	p.allocs["s00"] = 7
	p.allocs["s07"] = 3

	p.CleanupTenant("s07")
	ps := fm.paths()
	if len(ps) != 3 || ps[2] != "/feeder/ratios" {
		t.Fatalf("정책 B 는 ratios 재정규화 포함: %v", ps)
	}
	ratios := fm.calls[2]["ratios"].(map[string]interface{})
	if r := ratios["s00"].(float64); r != 1.0 {
		t.Fatalf("재정규화: 남은 7슬롯 단독 → 1.0 기대, got %v", r)
	}
}

func TestRestoreFromCheckpoint(t *testing.T) {
	fm := &feederMock{}
	srv := fm.server()
	defer srv.Close()
	p, admin := newTestModel(t, srv.URL)

	cp := `{"Data":{"PodDeviceEntries":[
	 {"PodUID":"u1","ResourceName":"keti.io/npu-resnet50",
	  "DeviceIDs":{"-1":["keti-npu0pe0-resnet50-s03","keti-npu0pe0-resnet50-s01"]}},
	 {"PodUID":"u2","ResourceName":"keti.re.kr/lsu",
	  "DeviceIDs":{"-1":["keti-gpu0-lsu5"]}}]}}`
	f := filepath.Join(t.TempDir(), "cp.json")
	os.WriteFile(f, []byte(cp), 0o644)

	p.RestoreFromCheckpoint(f, map[string]bool{"u1": true})
	<-admin // register 발신
	if !p.IsSlotRegistered("keti-npu0pe0-resnet50-s01") {
		t.Fatal("복원: canonical(정렬 최솟값) s01 기대")
	}
	if fm.calls[0]["time_ratio"].(float64) != 0.2 {
		t.Fatalf("복원 ratio = 2슬롯/10 = 0.2, got %v", fm.calls[0]["time_ratio"])
	}
	// GPU 리소스 엔트리는 무시돼야 함
	if p.IsSlotRegistered("keti-gpu0-lsu5") {
		t.Fatal("타 리소스 엔트리 오등록")
	}
}

func TestRestoreSkipsStaleEntries(t *testing.T) {
	fm := &feederMock{}
	srv := fm.server()
	defer srv.Close()
	p, _ := newTestModel(t, srv.URL)
	cp := `{"Data":{"PodDeviceEntries":[
	 {"PodUID":"dead","ResourceName":"keti.io/npu-resnet50",
	  "DeviceIDs":{"-1":["keti-npu0pe0-resnet50-s01"]}}]}}`
	f := filepath.Join(t.TempDir(), "cp.json")
	os.WriteFile(f, []byte(cp), 0o644)
	p.RestoreFromCheckpoint(f, map[string]bool{"live-other": true})
	if p.IsSlotRegistered("keti-npu0pe0-resnet50-s01") {
		t.Fatal("★stale 엔트리(삭제된 파드)가 재등록됨 — 고아 생성")
	}
	if len(fm.paths()) != 0 {
		t.Fatalf("stale 엔트리에 feeder 호출 발생: %v", fm.paths())
	}
}

func TestWatcherNpuCanonical(t *testing.T) {
	var got []string
	w := &PodWatcher{
		podToDevices:  map[string][]string{},
		podToNpuSlots: map[string][]string{"uid1": {"s07", "s03", "s05"}},
		stopCh:        make(chan struct{}),
	}
	w.SetNPUWatch("keti.io/npu-resnet50", func(c string) { got = append(got, c) })
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: "p", Namespace: "ns", UID: types.UID("uid1")}}
	w.deregisterForPod(pod)
	if len(got) != 1 || got[0] != "s03" {
		t.Fatalf("canonical 1건(정렬 최솟값 s03)만 기대: %v", got)
	}
	// 이중 정리 방지
	w.deregisterForPod(pod)
	if len(got) != 1 {
		t.Fatal("중복 정리 발생")
	}
}
