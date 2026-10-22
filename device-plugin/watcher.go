package main

import (
	"context"
	"encoding/json"
	stderrors "errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	// kubeletCheckpointPath is the kubelet internal checkpoint that maps
	// pod UIDs to allocated device IDs for each resource.
	kubeletCheckpointPath = "/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint"
)

// checkpointData is the subset of the kubelet checkpoint we need.
// Schema reference: k8s.io/kubernetes/pkg/kubelet/cm/devicemanager/checkpoint/checkpoint.go
type checkpointData struct {
	Data struct {
		PodDeviceEntries []podDeviceEntry `json:"PodDeviceEntries"`
	} `json:"Data"`
}

type podDeviceEntry struct {
	PodUID        string `json:"PodUID"`
	ContainerName string `json:"ContainerName"`
	ResourceName  string `json:"ResourceName"`
	// DeviceIDs is keyed by NUMA node index (e.g. "-1" means no affinity).
	// Each value is the list of device IDs allocated to that NUMA node.
	DeviceIDs map[string][]string `json:"DeviceIDs"`
}

// allDeviceIDs flattens the NUMA-keyed map into a single slice.
func (e *podDeviceEntry) allDeviceIDs() []string {
	var ids []string
	for _, devs := range e.DeviceIDs {
		ids = append(ids, devs...)
	}
	return ids
}

// PodWatcher watches the kubelet checkpoint and K8s Pod events to detect
// when a pod using our resource has terminated, then calls the controller
// to deregister the associated tenant slice.
type PodWatcher struct {
	resourceName string
	controller   *ControllerClient
	// isRegistered/forget: plugin 의 allocations 조회 콜백 (Exp_41 —
	// 등록에 실제 사용된 canonical ID 로만 deregister). nil 이면 폴백 경로.
	isRegistered  func(string) bool
	listAllocated func(time.Duration) []string // [Exp_97] 재조정 루프 입력(유예 경과분만)
	forget        func(string)
	// [Exp_134] Booster 자동 등록이 켜졌을 때만 해제 호출을 낸다.
	booster       bool
	k8sClient     kubernetes.Interface

	// [Exp_110 R-1] class 선기록용 — plugin 의 bless 소켓 디렉터리.
	//   비어 있으면 선기록을 하지 않는다(기존 동작).
	blessSockDir string
	classPending map[string]string // podUID → class (매핑 대기 중, 5초 틱에서 재시도)
	// [Exp_126 4-B] podUID → "<ns>/<name>/<container>". class 와 같은 이유로
	//   대기열이 필요하다 — Allocate 직후에는 kubelet 체크포인트에 devID 매핑이
	//   아직 없어서 곧바로 못 쓴다(실측: podref 0개). 5초 틱에서 재시도한다.
	podrefPending map[string]string
	// [Exp_112 3부] devID → 그 class 파일을 마지막으로 쓴 podUID(세대 식별자).
	//   ★삭제가 devID 기준이라 세대 구분이 없었다: 파드 종료 이벤트는 지연되므로,
	//     같은 devID 를 이미 받은 **새 파드의 선기록을 뒤늦게 지운다**
	//     (Exp_110 실측 11건 — 선기록 44초 뒤 "class 미도착" 경고).
	//   소유자가 일치할 때만 지운다. 파드 UID 는 K8s 가 보장하는 고유값이라
	//   별도 세대 카운터가 필요 없다.
	classOwner map[string]string
	classMu      sync.Mutex

	// [Exp_140 3부] SliceCreated 발행 여부 (podUID 기준). 로컬 중복 방지용 —
	//   플러그인 재기동으로 이 맵이 비어도 이벤트의 결정적 이름
	//   (kraken-slice-created-<podUID>)이 API 409 로 걸러 멱등이다.
	sliceCreatedSent map[string]bool

	// podToDevices maps podUID → set of deviceIDs allocated to that pod
	podToDevices   map[string][]string
	podToDevicesMu sync.Mutex

	// NPU 모델 슬롯 추적 (Exp_53): checkpoint 의 ResourceName 으로 식별 —
	// 디바이스 노드가 없어도 슬롯 ID 가 checkpoint 에 남으므로 GPU 와 동일
	// 메커니즘으로 소멸 감지가 가능하다 (Phase 0-3 확인).
	npuResource string       // 예: keti.io/npu-resnet50 ("" = 비활성, 첫 모델)
	npuCleanup  func(string) // canonical 슬롯 테넌트 정리 콜백 (첫 모델)
	// Exp_59 다중 모델: 리소스명 → 정리 콜백. 슬롯 ID 접두가 PE·모델을 담아
	// 유일하므로 podToNpuSlots 는 모델 구분 없이 공유해도 충돌하지 않는다.
	npuCleanups   map[string]func(string)
	podToNpuSlots map[string][]string
	// podToNpuRes: podUID → 그 파드가 쓴 NPU 모델 리소스명 (정리 콜백 선택용)
	podToNpuRes map[string]string

	stopCh chan struct{}
}

// NewPodWatcher builds a PodWatcher.  kubeconfig may be empty for in-cluster auth.
func NewPodWatcher(resourceName string, ctrl *ControllerClient, kubeconfig string) (*PodWatcher, error) {
	cfg, err := buildRestConfig(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("kubeconfig 빌드 실패: %w", err)
	}

	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("K8s 클라이언트 생성 실패: %w", err)
	}

	return &PodWatcher{
		resourceName:  resourceName,
		controller:    ctrl,
		k8sClient:     client,
		podToDevices:  make(map[string][]string),
		classPending:  make(map[string]string),
		podrefPending: make(map[string]string),
		classOwner:    make(map[string]string),
		sliceCreatedSent: make(map[string]bool),
		podToNpuSlots: make(map[string][]string),
		podToNpuRes:   make(map[string]string),
		npuCleanups:   make(map[string]func(string)),
		stopCh:        make(chan struct{}),
	}, nil
}

// Start launches the checkpoint reader and the K8s pod event watcher in separate goroutines.
func (w *PodWatcher) Start() {
	go w.runCheckpointSync()
	go w.runPodEventWatch()
	go w.runReconcile() // [Exp_97 Part D] 고아 슬롯 회수
}

// Stop signals both goroutines to exit.
func (w *PodWatcher) Stop() {
	close(w.stopCh)
}

// runCheckpointSync periodically re-reads the kubelet checkpoint to refresh
// the podUID → deviceIDs mapping.  The checkpoint is the authoritative source
// for which pod was assigned which physical device slice.
func (w *PodWatcher) runCheckpointSync() {
	// [Exp_97] 15s → 5s. 체크포인트는 로컬 파일 읽기라 저렴하고,
	//   맵이 신선할수록 재조정 오판 위험이 준다.
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	// Run once immediately on start.
	w.syncCheckpoint()

	for {
		select {
		case <-w.stopCh:
			return
		case <-ticker.C:
			w.syncCheckpoint()
			w.retryPendingClasses() // [Exp_110 R-1]
		}
	}
}

func (w *PodWatcher) syncCheckpoint() {
	raw, err := os.ReadFile(kubeletCheckpointPath)
	if err != nil {
		// Non-fatal: checkpoint may not exist yet.
		return
	}

	var cp checkpointData
	if err := json.Unmarshal(raw, &cp); err != nil {
		log.Printf("[watcher] checkpoint 파싱 실패: %v", err)
		return
	}

	w.podToDevicesMu.Lock()
	defer w.podToDevicesMu.Unlock()

	// Rebuild from checkpoint (only entries for our resources).
	fresh := make(map[string][]string)
	freshNpu := make(map[string][]string)
	freshNpuRes := make(map[string]string)
	for _, entry := range cp.Data.PodDeviceEntries {
		if entry.ResourceName == w.resourceName {
			fresh[entry.PodUID] = append(fresh[entry.PodUID], entry.allDeviceIDs()...)
			continue
		}
		// Exp_59: 등록된 NPU 모델 리소스 전부 추적 (단일 모델이면 종전과 동일).
		if w.npuResourceTracked(entry.ResourceName) {
			freshNpu[entry.PodUID] = append(freshNpu[entry.PodUID],
				entry.allDeviceIDs()...)
			freshNpuRes[entry.PodUID] = entry.ResourceName
		}
	}
	w.podToDevices = fresh
	w.podToNpuSlots = freshNpu
	w.podToNpuRes = freshNpuRes
}

// runPodEventWatch uses a K8s Watch to detect pod phase changes.
// When a pod reaches Succeeded or Failed it calls deregisterForPod().
func (w *PodWatcher) runPodEventWatch() {
	for {
		select {
		case <-w.stopCh:
			return
		default:
		}

		if err := w.watchOnce(); err != nil {
			log.Printf("[watcher] pod watch 오류: %v — 5초 후 재시도", err)
			select {
			case <-w.stopCh:
				return
			case <-time.After(5 * time.Second):
			}
		}
	}
}

func (w *PodWatcher) watchOnce() error {
	// 노드 자격증명(system:node:*)은 spec.nodeName 셀렉터 없는 클러스터 전역
	// watch 를 거부한다 (NodeAuthorizer). NODE_NAME 이 있으면 자기 노드로 한정.
	opts := metav1.ListOptions{Watch: true}
	if n := os.Getenv("NODE_NAME"); n != "" {
		opts.FieldSelector = "spec.nodeName=" + n
	}
	watcher, err := w.k8sClient.CoreV1().Pods("").Watch(
		context.Background(), opts)
	if err != nil {
		return fmt.Errorf("Watch 시작 실패: %w", err)
	}
	defer watcher.Stop()

	for {
		select {
		case <-w.stopCh:
			return nil
		case event, ok := <-watcher.ResultChan():
			if !ok {
				return fmt.Errorf("watch channel 닫힘")
			}
			pod, ok := event.Object.(*corev1.Pod)
			if !ok {
				continue
			}
			// [Exp_110 R-1] ★class 선기록 — libbless init 을 기다리지 않는다.
			//   근본 원인(Exp_109): libbless init 이 첫 CUDA 호출 시점에 일어나므로
			//   소켓도 class 도 그때 생긴다. 콜드 스타트에서 20초 가까이 걸려
			//   짝짓기 관측 창(~22초)을 넘겼다. plugin 은 파드 env 를 즉시 볼 수 있으므로
			//   여기서 미리 써두면 경합 자체가 사라진다.
			if event.Type == watch.Added || event.Type == watch.Modified {
				w.writeClassForPod(pod)
				w.writePodRefForPod(pod)   // [Exp_126 4-B]
			}
			if event.Type != watch.Modified && event.Type != watch.Deleted {
				continue
			}
			if isPodTerminated(pod) {
				w.deregisterForPod(pod)
			}
		}
	}
}

// retryPendingClasses re-attempts pre-writes whose device mapping was not yet
// in the kubelet checkpoint. Called from the 5s checkpoint sync tick. [Exp_110 R-1]
func (w *PodWatcher) retryPendingClasses() {
	if w.blessSockDir == "" {
		return
	}
	w.classMu.Lock()
	pend := make(map[string]string, len(w.classPending))
	for k, v := range w.classPending {
		pend[k] = v
	}
	w.classMu.Unlock()
	for uid, cls := range pend {
		w.podToDevicesMu.Lock()
		devs := append([]string(nil), w.podToDevices[uid]...)
		w.podToDevicesMu.Unlock()
		if len(devs) == 0 {
			continue
		}
		for _, devID := range devs {
			w.writeClassFile(devID, cls, uid)
			w.classMu.Lock()
			w.classOwner[devID] = uid
			w.classMu.Unlock()
		}
		w.classMu.Lock()
		delete(w.classPending, uid)
		w.classMu.Unlock()
	}
	// [Exp_126 4-B] podref 도 같은 틱에서 재시도한다
	w.classMu.Lock()
	pref := make(map[string]string, len(w.podrefPending))
	for k, v := range w.podrefPending {
		pref[k] = v
	}
	w.classMu.Unlock()
	for uid, ref := range pref {
		w.podToDevicesMu.Lock()
		devs := append([]string(nil), w.podToDevices[uid]...)
		w.podToDevicesMu.Unlock()
		if len(devs) == 0 {
			continue
		}
		for _, devID := range devs {
			w.writePodRefFile(devID, ref)
		}
		w.classMu.Lock()
		delete(w.podrefPending, uid)
		w.classMu.Unlock()
		// [Exp_140 3부] 대기열 경유로 매핑이 선 경우도 같은 지점 — ref 는
		// "<ns>/<name>/<container>" (writePodRefForPod 가 만든 형식).
		if parts := strings.SplitN(ref, "/", 3); len(parts) == 3 {
			w.emitSliceEvent("SliceCreated", parts[0], parts[1], uid, devs)
		}
	}
}

// SetBlessSockDir wires the plugin's socket dir so the watcher can pre-write
// the workload class file. [Exp_110 R-1]
func (w *PodWatcher) SetBlessSockDir(dir string) { w.blessSockDir = dir }

// emitSliceEvent posts SliceCreated/SliceDestroyed through the controller's
// mode_event route (Exp_140 3부 — A-4: involvedObject=Pod). 발행은 비동기이고
// 실패는 로그만 남긴다 — Allocate·해제 흐름을 막지 않는다.
//   created 는 podUID 로컬 중복 방지 + 서버 쪽 결정적 이름(409)의 이중 방어,
//   destroyed 는 deregisterForPod 가 파드당 1회만 도달하므로(매핑 삭제 후 조기
//   반환) 결정적 이름만으로 충분하다.
func (w *PodWatcher) emitSliceEvent(kind, ns, name, uid string, devs []string) {
	if w.controller == nil {
		return
	}
	if kind == "SliceCreated" {
		w.classMu.Lock()
		if w.sliceCreatedSent[uid] {
			w.classMu.Unlock()
			return
		}
		w.sliceCreatedSent[uid] = true
		w.classMu.Unlock()
	} else {
		w.classMu.Lock()
		delete(w.sliceCreatedSent, uid) // 맵 크기 유계 유지
		w.classMu.Unlock()
	}
	tenant := strings.Join(devs, ",")
	detail := fmt.Sprintf("devices=%d", len(devs))
	go func() {
		if err := w.controller.SliceEvent(kind, tenant, ns, name, uid, detail); err != nil {
			log.Printf("[watcher][경고] %s 발행 실패 pod=%s/%s: %v — "+
				"제어·해제에는 영향 없음 (T-5: 조용히 넘기지 않음)",
				kind, ns, name, err)
		}
	}()
}

// writeClassForPod pre-writes <sockDir>/<devID>/class from the pod's
// KRAKEN_WORKLOAD_CLASS env. [Exp_110 R-1]
//
//   ★libbless 도 init 시 같은 파일을 쓴다. 두 값은 **같은 env 에서 오므로 동일**하다.
//     따라서 덮어써도 무해하며, 늦게 쓰는 쪽(libbless)이 이겨도 값이 바뀌지 않는다.
//   ★env 가 없는 파드는 아무 것도 쓰지 않는다 — 기존처럼 미선언(= strict) 으로 남는다.
func (w *PodWatcher) writeClassForPod(pod *corev1.Pod) {
	if w.blessSockDir == "" {
		return
	}
	cls := ""
	for _, c := range pod.Spec.Containers {
		for _, e := range c.Env {
			if e.Name == "KRAKEN_WORKLOAD_CLASS" && (e.Value == "compute" || e.Value == "memory") {
				cls = e.Value
			}
		}
	}
	if cls == "" {
		return // 미선언 — 손대지 않는다(종료 시 삭제가 오염을 담당)
	}
	uid := string(pod.UID)
	w.podToDevicesMu.Lock()
	devs := append([]string(nil), w.podToDevices[uid]...)
	w.podToDevicesMu.Unlock()
	if len(devs) == 0 {
		// 아직 kubelet 체크포인트에 매핑이 없다(Allocate 직후). 대기열에 넣고
		// 5초 체크포인트 동기화 틱에서 재시도한다 — 파드 이벤트를 기다리지 않는다.
		w.classMu.Lock()
		w.classPending[uid] = cls
		w.classMu.Unlock()
		return
	}
	w.classMu.Lock()
	delete(w.classPending, uid)
	w.classMu.Unlock()
	for _, devID := range devs {
		w.writeClassFile(devID, cls, pod.Name)
		w.classMu.Lock()
		w.classOwner[devID] = uid
		w.classMu.Unlock()
	}
}

// writePodRefForPod pre-writes <sockDir>/<devID>/podref = "<ns>/<name>/<container>".
// [Exp_126 4-B] libbless 는 통계 일부를 **stderr 에만** 낸다(mem/ctx/queue/syscall/sm) —
//   컨테이너 안이라 컨트롤러가 못 읽는다(Exp_125 §3-A). 컨테이너 런타임이 stderr 을
//   잡으므로 **K8s 로그 API로 읽으면 libbless 를 고치지 않고 확보된다.**
//   그러려면 컨트롤러가 devID → 파드를 알아야 하는데, 그 매핑은 여기(watcher)에만 있다.
//   class 선기록과 **같은 파일 패턴**을 쓴다 — 새 통신 경로를 만들지 않는다.
func (w *PodWatcher) writePodRefForPod(pod *corev1.Pod) {
	if w.blessSockDir == "" {
		return
	}
	cname := ""
	for i := range pod.Spec.Containers {
		for name := range pod.Spec.Containers[i].Resources.Limits {
			if string(name) == w.resourceName {
				cname = pod.Spec.Containers[i].Name
			}
		}
	}
	if cname == "" {
		return // 우리 리소스를 요청하지 않은 파드 — 손대지 않는다
	}
	uid := string(pod.UID)
	w.podToDevicesMu.Lock()
	devs := append([]string(nil), w.podToDevices[uid]...)
	w.podToDevicesMu.Unlock()
	ref := pod.Namespace + "/" + pod.Name + "/" + cname
	if len(devs) == 0 {
		// 아직 체크포인트에 매핑이 없다 — class 선기록과 같은 대기열 경로를 탄다
		w.classMu.Lock()
		w.podrefPending[uid] = ref
		w.classMu.Unlock()
		return
	}
	w.classMu.Lock()
	delete(w.podrefPending, uid)
	w.classMu.Unlock()
	for _, devID := range devs {
		w.writePodRefFile(devID, ref)
	}
	// [Exp_140 3부] 파드↔디바이스 매핑이 선 시점 = 슬라이스가 파드에 귀속된 시점.
	//   Allocate 자체에는 파드 정체가 없어(디바이스 플러그인 API 한계) 여기서 낸다.
	w.emitSliceEvent("SliceCreated", pod.Namespace, pod.Name, uid, devs)
}

// writePodRefFile writes one <sockDir>/<devID>/podref. [Exp_126 4-B]
//   class 선기록과 같은 이유로 **내용이 다르면 다시 쓴다** — devID 는 파드가 끝나면
//   풀에 돌아가 다음 파드가 같은 ID 를 받는다(Exp_110 R-1 실사고).
func (w *PodWatcher) writePodRefFile(devID, ref string) {
	dir := filepath.Join(w.blessSockDir, devID)
	f := filepath.Join(dir, "podref")
	if b, err := os.ReadFile(f); err == nil && strings.TrimSpace(string(b)) == ref {
		return
	}
	if err := os.MkdirAll(dir, 0o777); err != nil {
		log.Printf("[watcher][경고] podref 디렉터리 생성 실패 %s: %v — "+
			"stderr 신호 수집이 이 테넌트에서 비활성된다", dir, err)
		return
	}
	if err := os.WriteFile(f, []byte(ref+"\n"), 0o666); err != nil {
		// [Exp_107 T-5] 조용한 폴백 금지
		log.Printf("[watcher][경고] podref 기록 실패 %s: %v — "+
			"stderr 신호 수집이 이 테넌트에서 비활성된다", f, err)
		return
	}
	log.Printf("[watcher] podref 선기록: %s → %s", devID, ref)
}

// writeClassFile writes one <sockDir>/<devID>/class file. [Exp_110 R-1]
//
//   ★중복 쓰기 방지를 devID 단위로 걸면 안 된다. devID 는 파드가 끝나면 풀에 돌아가
//     다음 파드가 **같은 ID 를 다시 받는다**. 한 번 썼다고 건너뛰면 이전 회차의 class 가
//     그대로 남아 반대 성격으로 오판된다(실측: 회차 2에서 시간 축 비 5.89, relaxed 실패).
//     그래서 내용이 다르면 다시 쓴다 — 파일 쓰기는 값싸고 멱등하다.
func (w *PodWatcher) writeClassFile(devID, cls, who string) {
	dir := filepath.Join(w.blessSockDir, devID)
	if b, err := os.ReadFile(filepath.Join(dir, "class")); err == nil &&
		strings.TrimSpace(string(b)) == cls {
		return // 이미 같은 값 — 로그만 줄인다
	}
	if err := os.MkdirAll(dir, 0o777); err != nil {
		log.Printf("[watcher][경고] class 선기록 디렉터리 생성 실패 %s: %v — "+
			"libbless init 을 기다리는 기존 경로로 폴백된다", dir, err)
		return
	}
	f := filepath.Join(dir, "class")
	if err := os.WriteFile(f, []byte(cls+"\n"), 0o666); err != nil {
		// [Exp_107 T-5] 조용한 폴백 금지
		log.Printf("[watcher][경고] class 선기록 실패 %s: %v — "+
			"libbless init 을 기다리는 기존 경로로 폴백된다", f, err)
		return
	}
	log.Printf("[watcher] class 선기록: %s → %s (%s)", devID, cls, who)
}

// isPodTerminated returns true when a pod has reached a terminal phase
// and all containers have exited.
func isPodTerminated(pod *corev1.Pod) bool {
	if pod.Status.Phase == corev1.PodSucceeded ||
		pod.Status.Phase == corev1.PodFailed {
		return true
	}
	// Catch pods deleted while still running (DeletionTimestamp set, no containers left running).
	if pod.DeletionTimestamp != nil {
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Running != nil {
				return false
			}
		}
		return true
	}
	return false
}

// SetRegistryLookup wires the plugin's allocation map into the watcher so
// deregisterForPod can target only canonical(등록된) IDs (Exp_41 근본 수정).
func (w *PodWatcher) SetRegistryLookup(isRegistered func(string) bool,
	forget func(string)) {
	w.isRegistered = isRegistered
	w.forget = forget
}

// SetAllocatedLister wires the plugin's full allocation snapshot into the watcher.
// [Exp_97 Part D] 재조정 루프의 입력 — 이것이 없으면 루프는 동작하지 않는다(무해).
func (w *PodWatcher) SetAllocatedLister(list func(time.Duration) []string) {
	w.listAllocated = list
}

// SetNPUWatch enables NPU model-slot tracking (Exp_53).
// cleanup 은 canonical 슬롯 ID 1건만 받는다 (Exp_41 위생 — N-1 노이즈 금지).
// Exp_59: 다중 모델 — 모델별로 반복 호출하면 리소스별 정리 콜백이 누적된다
// (npuResource/npuCleanup 단일 필드는 첫 호출 값으로 유지 = 하위호환).
func (w *PodWatcher) SetNPUWatch(resource string, cleanup func(string)) {
	if w.npuCleanups == nil {
		w.npuCleanups = make(map[string]func(string))
	}
	w.npuCleanups[resource] = cleanup
	if w.npuResource == "" {
		w.npuResource = resource
		w.npuCleanup = cleanup
	}
}

// npuResourceTracked reports whether the checkpoint entry belongs to any
// registered NPU model resource (Exp_59 다중 모델).
func (w *PodWatcher) npuResourceTracked(resource string) bool {
	if resource == "" {
		return false
	}
	if _, ok := w.npuCleanups[resource]; ok {
		return true
	}
	return resource == w.npuResource && w.npuResource != ""
}

// deregisterForPod looks up the device IDs assigned to the pod in the
// checkpoint and calls the controller to free each slice.
// podWantsOurResource reports whether the pod actually requested the resource we manage.
// [Exp_97] NPU·CDI 등 우리 리소스와 무관한 파드까지 경고를 찍으면 노이즈가 된다.
func (w *PodWatcher) podWantsOurResource(pod *corev1.Pod) bool {
	for i := range pod.Spec.Containers {
		for name := range pod.Spec.Containers[i].Resources.Limits {
			if string(name) == w.resourceName {
				return true
			}
		}
	}
	return false
}

func (w *PodWatcher) deregisterForPod(pod *corev1.Pod) {
	// NPU 모델 슬롯 정리 (Exp_53) — canonical(정렬 최솟값) 1건만.
	w.podToDevicesMu.Lock()
	slots, hasNpu := w.podToNpuSlots[string(pod.UID)]
	npuRes := w.podToNpuRes[string(pod.UID)]
	if hasNpu {
		delete(w.podToNpuSlots, string(pod.UID))
		delete(w.podToNpuRes, string(pod.UID))
	}
	w.podToDevicesMu.Unlock()
	if hasNpu && len(slots) > 0 {
		// Exp_59: 그 파드가 쓴 모델 리소스의 콜백으로 라우팅 (미상이면 첫 모델).
		cleanup := w.npuCleanup
		if cb, ok := w.npuCleanups[npuRes]; ok && cb != nil {
			cleanup = cb
		}
		if cleanup != nil {
			sort.Strings(slots)
			log.Printf("[watcher] NPU 슬롯 정리: pod=%s/%s tenant=%s resource=%s (슬롯 %d개)",
				pod.Namespace, pod.Name, slots[0], npuRes, len(slots))
			cleanup(slots[0])
		}
	}

	w.podToDevicesMu.Lock()
	devices, ok := w.podToDevices[string(pod.UID)]
	w.podToDevicesMu.Unlock()
	if !ok {
		// [Exp_97 Part D] ★구 동작은 여기서 로그 없이 return 했다 → SHM 슬롯 영구 누수.
		//   15초 체크포인트 재구축이 파드 종료 이벤트보다 먼저 돌면 매핑이 사라진다
		//   (--force 삭제에서 특히 잦다, Exp_96 §9). 조용히 넘기지 않고 폴백한다:
		//   체크포인트를 즉시 다시 읽어 이 파드의 ID 를 찾는다. 그래도 없으면 경고만 남기고
		//   재조정 루프(runReconcile)가 나중에 회수한다.
		w.syncCheckpoint()
		w.podToDevicesMu.Lock()
		devices, ok = w.podToDevices[string(pod.UID)]
		w.podToDevicesMu.Unlock()
		if !ok {
			if w.podWantsOurResource(pod) {
				log.Printf("[watcher] ⚠ 매핑 없음 (pod=%s/%s uid=%s) — 체크포인트 재조회도 실패. "+
					"재조정 루프에 맡긴다", pod.Namespace, pod.Name, pod.UID)
			}
			return
		}
		log.Printf("[watcher] 매핑 폴백 성공 (pod=%s/%s, %d개 ID)",
			pod.Namespace, pod.Name, len(devices))
	}
	// [Exp_110 R-1] devID 는 재사용된다 — 죽은 파드의 class 를 남기면 다음 파드가
	//   반대 성격으로 오판된다(실측: class 미선언 시간 축 파드가 memory 로 판정, 비 5.89).
	//   ★대안(파드 시작 시 미선언 파드가 지우기)을 시험했으나 6축 5회에서 0/5 로
	//     후퇴했다(raw/r1_5rounds_final.txt). 검증된 이 경로를 유지한다.
	if w.blessSockDir != "" {
		for _, devID := range devices {
			// [Exp_112 3부] 소유자가 이 파드일 때만 지운다. 이미 다음 파드가
			//   선기록했다면(종료 이벤트 지연) 그 기록을 보존한다.
			w.classMu.Lock()
			owner := w.classOwner[devID]
			mine := owner == "" || owner == string(pod.UID)
			if mine {
				delete(w.classOwner, devID)
			}
			w.classMu.Unlock()
			if !mine {
				log.Printf("[watcher] class 삭제 생략: %s — 이미 다음 파드(%s)가 소유 "+
					"(종료 이벤트 지연, pod=%s)", devID, owner[:8], pod.Name)
				continue
			}
			_ = os.Remove(filepath.Join(w.blessSockDir, devID, "class"))
			// [Exp_126 4-B] podref 도 함께 지운다. 남겨두면 devID 가 풀에 돌아가
			//   다음 파드가 같은 ID 를 받았을 때 **이전 파드의 로그를 읽는다**
			//   (실측: decode 회차에서 두 테넌트가 같은 파드를 가리켰다).
			//   class 와 같은 계열의 devID 재사용 오염(Exp_110 R-1)이다.
			_ = os.Remove(filepath.Join(w.blessSockDir, devID, "podref"))
		}
	}

	// Remove from local map so we don't deregister twice.
	w.podToDevicesMu.Lock()
	delete(w.podToDevices, string(pod.UID))
	w.podToDevicesMu.Unlock()

	// Exp_41 근본 수정: LSU 모드는 파드당 N개 device ID 중 1개(canonical)만
	// 등록된다. plugin allocations 조회로 그 ID만 선별 — N-1건의 404 시도
	// (Exp_33 의 88건 노이즈)를 원천 제거. plugin 재시작 등으로 조회가 비면
	// 전체 ID 폴백하되, 404(ErrTenantNotFound)는 정상 흐름으로 조용히 처리.
	targets := devices
	fallback := true
	if w.isRegistered != nil {
		var reg []string
		for _, id := range devices {
			if w.isRegistered(id) {
				reg = append(reg, id)
			}
		}
		if len(reg) > 0 {
			targets = reg
			fallback = false
		} else {
			log.Printf("[watcher] 등록 조회 0건 (pod=%s) — 전체 %d개 ID 폴백",
				pod.Name, len(devices))
		}
	}
	for _, deviceID := range targets {
		err := w.controller.DeregisterByTenantID(deviceID)
		switch {
		case err == nil:
			if w.forget != nil {
				w.forget(deviceID)
			}
			w.boosterForget(deviceID)
			log.Printf("[watcher] deregister 완료: pod=%s/%s device=%s",
				pod.Namespace, pod.Name, deviceID)
		case stderrors.Is(err, ErrTenantNotFound):
			if !fallback {
				// 선별 경로에서의 404 는 비정상 — 남긴다 (조용한 실패 금지)
				log.Printf("[watcher] deregister 404 (선별 경로 — 조사 필요) "+
					"pod=%s device=%s", pod.Name, deviceID)
			}
			// 폴백 경로의 404 는 정상 (canonical 아님) — 무로그
		default:
			log.Printf("[watcher] deregister 실패 (pod=%s device=%s): %v",
				pod.Name, deviceID, err)
		}
	}

	// [Exp_140 3부] 슬라이스 소멸 이벤트 — 파드 종료·점유 해제 시점.
	//   deregisterForPod 는 매핑 삭제 후 재진입 시 조기 반환하므로 파드당 1회다.
	//   (재조정 루프의 고아 회수는 파드 정체가 없어 이벤트를 내지 않는다 — report 기록)
	w.emitSliceEvent("SliceDestroyed", pod.Namespace, pod.Name, string(pod.UID), devices)
}

// LivePodUIDs lists non-terminated pods on this node (Exp_53 — 복원 필터).
// runReconcile periodically reclaims orphaned controller slots.
//
// [Exp_97 Part D] 왜 필요한가 (Exp_96 §9 실측):
//   SHM 테넌트 슬롯은 MAX_TENANTS=8 로 유한하다. 반납은 파드 종료 이벤트에만 의존하는데,
//   ① 15초 체크포인트 재구축이 종료 이벤트보다 먼저 돌거나 ② API 서버가 잠깐 끊기면
//   매핑이 사라져 반납이 누락된다. 그러면 8회 할당만에 **노드의 모든 GPU 파드가
//   UnexpectedAdmissionError 로 거부**되고, 재기동 전까지 자연 회복하지 않았다.
//
// 이 루프는 "플러그인이 할당했다고 믿는 device ID" 를 "실제 live 파드가 쓰는 ID" 와 대조해
// 어느 쪽에도 속하지 않는 고아를 회수한다. 이벤트를 놓쳤든 API 가 끊겼든 **상태 대조로
// 수렴**하므로 원인과 무관하게 회복한다.
//
// 안전장치:
//   · live 파드 조회가 실패하면 **아무 것도 하지 않는다**(nil 반환 → 전량 회수 방지).
//   · 체크포인트에 있는 ID 는 아직 kubelet 이 쥐고 있다는 뜻이므로 회수 대상에서 제외한다.
//   · 회수는 로그를 남긴다 — 조용한 회수는 조용한 누수만큼 나쁘다.
// reconcileGrace — 이보다 최근에 할당된 슬롯은 재조정 대상에서 제외한다.
//
// [Exp_97] 값 선정 근거(실측):
//   · 유예 0   → 할당 2초 만에 살아있는 슬롯을 회수했다(axis 시간축·relaxed 축 FAIL). 치명적.
//   · 유예 60s → 오회수는 없으나 MAX_TENANTS=8 이라 빠른 turnover(5s 주기)에서 8회만에 고갈(3/12 실패).
//   → reconcileOnce 가 매번 체크포인트를 **즉시 재조회**하므로 긴 유예는 불필요하다.
//     kubelet 이 Allocate 후 체크포인트에 기록하기까지의 지연만 덮으면 된다.
const reconcileGrace = 15 * time.Second

func (w *PodWatcher) runReconcile() {
	// 주기는 짧게 — MAX_TENANTS=8 뿐이라 turnover 가 빠르면 30s 도 길다.
	// 비용은 파드 list 1회/주기로 미미하다.
	const period = 10 * time.Second
	ticker := time.NewTicker(period)
	defer ticker.Stop()
	for {
		select {
		case <-w.stopCh:
			return
		case <-ticker.C:
			w.reconcileOnce()
		}
	}
}

// sweepPrewrites removes stale class/podref files for devIDs no live pod holds.
//   ★소켓 파일(*.sock)은 건드리지 않는다 — 죽은 소켓은 feeder 의 sockAlive() 활성
//     검사가 담당한다(Exp_112). 두 곳에서 같은 것을 관리하면 경합이 난다.
// boosterForget removes a tenant from the Turnaround Booster (Exp_134).
//
//	★해제를 빠뜨리면 devID 가 재사용됐을 때 **이전 파드의 생존 신호 경로**로
//	  판정한다. class·podref 가 종료 시 삭제를 두는 것과 같은 이유다
//	  (T-7 — Exp_126 §5-4 에서 이 삭제를 빠뜨려 사고가 났다).
func (w *PodWatcher) boosterForget(deviceID string) {
	if !w.booster || w.controller == nil {
		return
	}
	if err := w.controller.BoosterDeregister(deviceID); err != nil {
		log.Printf("[watcher][경고] Booster 해제 실패 device=%s: %v — "+
			"이 devID 가 재사용되면 이전 등록으로 판정될 수 있다", deviceID, err)
	}
}

func (w *PodWatcher) sweepPrewrites(inUse map[string]bool) {
	if w.blessSockDir == "" {
		return
	}
	ents, err := os.ReadDir(w.blessSockDir)
	if err != nil {
		rngdWarnOnceLike("sweep_dir", fmt.Sprintf(
			"선기록 정리: 디렉터리 조회 실패 %s: %v — 이번 주기는 건너뛴다", w.blessSockDir, err))
		return
	}
	cut := time.Now().Add(-reconcileGrace)
	n := 0
	for _, e := range ents {
		if !e.IsDir() || inUse[e.Name()] {
			continue
		}
		for _, f := range []string{"class", "podref"} {
			p := filepath.Join(w.blessSockDir, e.Name(), f)
			st, err := os.Stat(p)
			if err != nil || !st.ModTime().Before(cut) {
				continue // 없거나, 유예 안 — 건드리지 않는다
			}
			if err := os.Remove(p); err != nil {
				rngdWarnOnceLike("sweep_rm", fmt.Sprintf(
					"선기록 정리 실패 %s: %v — 낡은 값이 다음 파드로 샐 수 있다", p, err))
				continue
			}
			n++
		}
	}
	if n > 0 {
		log.Printf("[watcher] 선기록 정리: %d개 (유예 %s 경과, 점유 중 아님)", n, reconcileGrace)
	}
}

func (w *PodWatcher) reconcileOnce() {
	if w.listAllocated == nil || w.isRegistered == nil {
		return // 배선되지 않음 — 무해하게 건너뛴다
	}
	live := w.LivePodUIDs()
	if live == nil {
		// 조회 실패 → 판단 보류. 실패를 "파드 없음"으로 오판하면 살아있는 슬롯을 죽인다.
		return
	}
	// [Exp_97] 15초 주기를 기다리지 않고 즉시 최신화 — stale 맵으로 판정하면 오판한다.
	w.syncCheckpoint()
	// live 파드가 점유 중인 device ID 집합 (체크포인트 기준)
	inUse := make(map[string]bool)
	w.podToDevicesMu.Lock()
	for uid, ids := range w.podToDevices {
		if live[uid] {
			for _, id := range ids {
				inUse[id] = true
			}
		}
	}
	w.podToDevicesMu.Unlock()

	var orphans []string
	// [Exp_97] ★유예: 체크포인트 주기(15s)보다 넉넉히 크게. 갓 할당된 슬롯을 건드리지 않는다.
	for _, id := range w.listAllocated(reconcileGrace) {
		if !inUse[id] {
			orphans = append(orphans, id)
		}
	}
	// [Exp_127 4부] 선기록 파일 정리. 파드를 --force 로 지우면 종료 이벤트 경로를
	//   타지 않아 class/podref 가 남는다(실측: class 177개, podref 159개).
	//   ★안전장치 둘 — 유래를 함께 적는다(T-7):
	//     ① live 조회 실패면 이 함수가 위에서 이미 return 한다. 실패를 "파드 없음"으로
	//        읽고 전량 삭제하면 **살아있는 파드의 class 가 사라진다.** libbless 는 init
	//        때 한 번만 쓰므로 복구되지 않고, 짝짓기가 strict 로 무너진다
	//        (Exp_110 R-1 이 실사고로 얻은 것과 같은 결과).
	//     ② mtime 유예 — 갓 할당된 devID 의 선기록을 지우면 같은 사고가 난다.
	//        reconcileGrace 를 그대로 쓴다(새 상수를 만들지 않는다).
	w.sweepPrewrites(inUse)
	if len(orphans) == 0 {
		return
	}
	log.Printf("[watcher] 재조정: 고아 슬롯 %d개 발견 — 회수 시도 %v", len(orphans), orphans)
	for _, id := range orphans {
		err := w.controller.DeregisterByTenantID(id)
		switch {
		case err == nil:
			if w.forget != nil {
				w.forget(id)
			}
			w.boosterForget(id)
			log.Printf("[watcher] 재조정 회수 완료: %s", id)
		case stderrors.Is(err, ErrTenantNotFound):
			// 컨트롤러엔 이미 없다 — 로컬 기록만 정리
			if w.forget != nil {
				w.forget(id)
			}
		default:
			log.Printf("[watcher] 재조정 회수 실패: %s: %v (다음 주기 재시도)", id, err)
		}
	}
}

func (w *PodWatcher) LivePodUIDs() map[string]bool {
	opts := metav1.ListOptions{}
	if n := os.Getenv("NODE_NAME"); n != "" {
		opts.FieldSelector = "spec.nodeName=" + n
	}
	pods, err := w.k8sClient.CoreV1().Pods("").List(context.Background(), opts)
	if err != nil {
		log.Printf("[watcher] LivePodUIDs 실패: %v — 필터 없이 진행", err)
		return nil
	}
	out := make(map[string]bool, len(pods.Items))
	for i := range pods.Items {
		if !isPodTerminated(&pods.Items[i]) {
			out[string(pods.Items[i].UID)] = true
		}
	}
	return out
}

// buildRestConfig creates a *rest.Config from kubeconfig path or in-cluster env.
func buildRestConfig(kubeconfig string) (*rest.Config, error) {
	if kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	// In-cluster (ServiceAccount token).
	cfg, err := rest.InClusterConfig()
	if err != nil {
		// Fallback: look for KUBECONFIG env var.
		if kc := os.Getenv("KUBECONFIG"); kc != "" {
			return clientcmd.BuildConfigFromFlags("", kc)
		}
		return nil, err
	}
	return cfg, nil
}

// rngdWarnOnceLike — 같은 사유는 1회만 (Exp_107 T-5). rngd 쪽 헬퍼와 같은 형태이나
//   패키지 내 중복 선언을 피하려 별도 이름을 쓴다.
var watcherWarned sync.Map

func rngdWarnOnceLike(key, msg string) {
	if _, dup := watcherWarned.LoadOrStore(key, true); !dup {
		log.Printf("[watcher][경고] %s", msg)
	}
}
