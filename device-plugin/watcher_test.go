package main

// Exp_41 — watcher 선별 deregister 결정성 테스트 (O-2/watcher 위생).
// LSU 모드: 파드당 N개 device ID 중 canonical 1개만 등록됨.
//   ① 조회 배선 시: canonical ID 1건만 POST (N-1건 노이즈 원천 제거)
//   ② 조회 0건(플러그인 재시작 등): 전체 폴백하되 404 는 조용히 처리
//   ③ 성공 시 forget 콜백으로 로컬 기록 정리

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8stypes "k8s.io/apimachinery/pkg/types"
)

func fakeControllerServer(t *testing.T, registered map[string]bool,
	calls *[]string, mu *sync.Mutex) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(
		func(rw http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/deregister_by_id" {
				rw.WriteHeader(http.StatusNotFound)
				return
			}
			b, _ := io.ReadAll(r.Body)
			var req deregisterByIDReq
			_ = json.Unmarshal(b, &req)
			mu.Lock()
			*calls = append(*calls, req.TenantID)
			mu.Unlock()
			if registered[req.TenantID] {
				rw.WriteHeader(http.StatusOK)
				_, _ = rw.Write([]byte(`{"ok":true}`))
				return
			}
			rw.WriteHeader(http.StatusNotFound)
			_, _ = rw.Write([]byte(`{"error":"tenant_id not found"}`))
		}))
}

func podWithUID(uid string) *corev1.Pod {
	return &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: "p-" + uid, Namespace: "test", UID: k8stypes.UID(uid)}}
}

// 89-unit LSU 할당 시나리오: canonical=lsu158, 나머지는 미등록 ID
func TestDeregisterSelective(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	registered := map[string]bool{"keti-gpu0-lsu158": true}
	ts := fakeControllerServer(t, registered, &calls, &mu)
	defer ts.Close()

	devices := []string{"keti-gpu0-lsu3", "keti-gpu0-lsu158", "keti-gpu0-lsu77"}
	var forgotten []string
	w := &PodWatcher{
		resourceName: "keti.re.kr/lsu",
		controller:   NewControllerClient(ts.URL),
		podToDevices: map[string][]string{"uid1": devices},
	}
	w.SetRegistryLookup(
		func(id string) bool { return registered[id] },
		func(id string) { forgotten = append(forgotten, id) })

	w.deregisterForPod(podWithUID("uid1"))

	if len(calls) != 1 || calls[0] != "keti-gpu0-lsu158" {
		t.Fatalf("선별 실패: POST 대상=%v (기대: canonical 1건)", calls)
	}
	if len(forgotten) != 1 || forgotten[0] != "keti-gpu0-lsu158" {
		t.Fatalf("forget 미호출: %v", forgotten)
	}
	if _, still := w.podToDevices["uid1"]; still {
		t.Fatalf("podToDevices 정리 안 됨")
	}
}

// 조회 0건 → 전체 폴백 + 404 는 오류 아님 (등록건만 성공)
func TestDeregisterFallbackAll(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	registered := map[string]bool{"keti-gpu0-lsu158": true}
	ts := fakeControllerServer(t, registered, &calls, &mu)
	defer ts.Close()

	devices := []string{"keti-gpu0-lsu3", "keti-gpu0-lsu158", "keti-gpu0-lsu77"}
	w := &PodWatcher{
		resourceName: "keti.re.kr/lsu",
		controller:   NewControllerClient(ts.URL),
		podToDevices: map[string][]string{"uid2": devices},
	}
	w.SetRegistryLookup(func(id string) bool { return false }, nil)

	w.deregisterForPod(podWithUID("uid2"))

	if len(calls) != len(devices) {
		t.Fatalf("폴백 시 전체 시도 기대: %v", calls)
	}
}

// 배선 없음(nil) — 구버전 동작 유지 (전체 시도)
func TestDeregisterNoLookupLegacy(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	ts := fakeControllerServer(t, map[string]bool{}, &calls, &mu)
	defer ts.Close()

	w := &PodWatcher{
		resourceName: "keti.re.kr/lsu",
		controller:   NewControllerClient(ts.URL),
		podToDevices: map[string][]string{"uid3": {"a", "b"}},
	}
	w.deregisterForPod(podWithUID("uid3"))
	if len(calls) != 2 {
		t.Fatalf("legacy 전체 시도 기대: %v", calls)
	}
}
