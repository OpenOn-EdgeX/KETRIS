package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

const (
	// kubeletSocket is the well-known socket path of the kubelet device-plugin manager.
	kubeletSocket = "/var/lib/kubelet/device-plugins/kubelet.sock"

	// pluginSocketDir is where the plugin places its own gRPC socket.
	pluginSocketDir = "/var/lib/kubelet/device-plugins"

	// pluginSocketName is the file name of our plugin socket.
	pluginSocketName = "keti-gpu-slice.sock"

	// shmBasePath is the tmpfs directory that holds POSIX shared memory files.
	shmBasePath = "/dev/shm"

	// krakenGroupID matches the group_id used when kraken_controller was started.
	krakenGroupID = "default"
)

// allocationRecord tracks what was registered in the controller for a device slice
// so the pod watcher can call Deregister with the correct tenant_idx.
type allocationRecord struct {
	tenantIdx int
	units     int       // 요청 LSU 수 (Exp_74 — 시간축 auto-arm ratio 를 LSU 비례로)
	at        time.Time // [Exp_97] 할당 시각 — 재조정 유예 판정용
}

// DevicePlugin implements the K8s Device Plugin v1beta1 gRPC interface.
type DevicePlugin struct {
	// Embed the unimplemented server to satisfy the gRPC interface contract
	// introduced in newer protoc-gen-go-grpc versions.
	pluginapi.UnimplementedDevicePluginServer

	resourceName string
	socketPath   string
	slicesPerGPU int

	// lsuMode advertises each GPU as `LSU` discrete units (resource
	// keti.re.kr/lsu) instead of `slicesPerGPU` equal slices.  A pod requests
	// an integer quantity of LSUs and Allocate maps that quantity to a
	// proportional virtual-SM / memory / MPS share.  When false the original
	// fixed-slice behaviour is preserved unchanged.
	lsuMode bool

	controller *ControllerClient
	gpus       []GPUInfo

	// ocFactor (Exp_73 이중제수) — 광고 LSU = 물리 LSU × ocFactor. 광고·메모리
	// 제수는 광고 LSU, 컴퓨트 pct 제수는 물리 LSU. 1.0 = 오버커밋 없음(기본).
	ocFactor float64

	// blessSO/blessSockDir enable libbless LD_PRELOAD injection on Allocate
	// (Exp_33 — 시분할 제어 경로). blessSockDir 은 컨테이너 /tmp 로 마운트되어
	// libbless 제어 소켓(/tmp/bless-<pid>.sock)을 호스트에 노출한다.
	blessSO      string
	blessSockDir string
	// mpsPipeDir enables MPS mode (Exp_71 Part B): 지정 시 Allocate 가 파이프
	// 디렉토리를 마운트하고 BLESS_SPACE_MODE=mps_env 를 주입한다 — 공간 제한을
	// MPS 서버가 강제(Exp_61 (B) 구성). 비우면 기존(ctx affinity 폴백) 그대로.
	mpsPipeDir string

	// [Exp_131] tracerSO/cuptiDir enable CUPTI 커널 관측 주입 (기본 꺼짐).
	//   tracerSO   = kernel_tracer.so 호스트 경로 (LD_PRELOAD 에 libbless 뒤로 붙음)
	//   cuptiDir   = libcupti.so.12 가 있는 호스트 디렉토리
	// libcupti 는 LD_LIBRARY_PATH 가 아니라 **LD_PRELOAD 로 함께 싣는다** —
	// 워크로드가 이미 쓰고 있을 LD_LIBRARY_PATH 를 Allocate 가 덮어쓰지 않기
	// 위함이다(Allocate 는 파드의 기존 env 를 읽을 수 없다).
	tracerSO      string
	cuptiDir      string
	tracerFlushMS int
	tracerBufKB   int

	// [Exp_134] fastTeardownDir = sitecustomize.py 가 든 호스트 디렉터리.
	//   지정 시 GPU Allocate 가 PYTHONPATH + BLESS_FAST_TEARDOWN=1 을 주입한다.
	//   기본 빈 값 = 꺼짐(마운트도 env 도 없다 — Exp_131 tracer 와 같은 규약).
	fastTeardownDir string

	// [Exp_134] boosterRegister — Allocate 시 Booster 자동 등록 (KRAKEN_BOOSTER=1).
	boosterRegister bool

	// devices maps device_id → *pluginapi.Device (authoritative state for ListAndWatch)
	devices   map[string]*pluginapi.Device
	devicesMu sync.RWMutex

	// allocations maps device_id → allocationRecord for in-flight allocations
	allocations sync.Map

	server   *grpc.Server
	updateCh chan struct{}
	stopCh   chan struct{}
	wg       sync.WaitGroup
}

// NewDevicePlugin initialises the plugin.  It queries the controller for GPU
// physical specs and enumerates GPUs via nvidia-smi.
func NewDevicePlugin(
	resourceName string,
	slicesPerGPU int,
	lsuMode bool,
	controller *ControllerClient,
	gpus []GPUInfo,
) *DevicePlugin {
	p := &DevicePlugin{
		resourceName: resourceName,
		socketPath:   filepath.Join(pluginSocketDir, pluginSocketName),
		slicesPerGPU: slicesPerGPU,
		lsuMode:      lsuMode,
		controller:   controller,
		gpus:         gpus,
		ocFactor:     1.0,
		devices:      make(map[string]*pluginapi.Device),
		updateCh:     make(chan struct{}, 1),
		stopCh:       make(chan struct{}),
	}

	p.rebuildDevices()
	return p
}

// SetOvercommitFactor updates the factor and rebuilds the advertised device set
// (Exp_73 — factor 는 catalog 로드 후 설정되므로 광고 device 맵을 다시 만든다).
func (p *DevicePlugin) SetOvercommitFactor(f float64) {
	if f > 0 {
		p.ocFactor = f
	}
	p.rebuildDevices()
}

// rebuildDevices repopulates the advertised device map from current gpus+ocFactor.
func (p *DevicePlugin) rebuildDevices() {
	p.devicesMu.Lock()
	defer p.devicesMu.Unlock()
	p.devices = make(map[string]*pluginapi.Device)
	for _, gpu := range p.gpus {
		for _, id := range p.deviceIDsForGPU(gpu) {
			p.devices[id] = &pluginapi.Device{ID: id, Health: pluginapi.Healthy}
		}
	}
}

// physicalUnits returns the GPU's measured physical LSU (컴퓨트 pct 제수).
// 이것이 컴퓨트 100% 의 기준 — ΣreqUnits 가 이걸 넘으면 Σpct>100%(오버커밋).
func (p *DevicePlugin) physicalUnits(gpu GPUInfo) int {
	if p.lsuMode {
		return gpu.LSU
	}
	return p.slicesPerGPU
}

// unitsPerGPU returns how many advertised device IDs a GPU exposes (광고 LSU).
// LSU 모드에서 광고 LSU = round(물리 LSU × ocFactor) — factor>1 이면 물리보다
// 많이 광고(오버커밋). 광고량과 메모리 quota 제수가 이 값이다.
func (p *DevicePlugin) unitsPerGPU(gpu GPUInfo) int {
	if p.lsuMode {
		n := int(float64(gpu.LSU)*p.ocFactor + 0.5)
		if n < 1 {
			n = 1
		}
		return n
	}
	return p.slicesPerGPU
}

// deviceIDsForGPU enumerates the device IDs advertised for one GPU.
func (p *DevicePlugin) deviceIDsForGPU(gpu GPUInfo) []string {
	n := p.unitsPerGPU(gpu)
	ids := make([]string, 0, n)
	for i := 0; i < n; i++ {
		if p.lsuMode {
			ids = append(ids, lsuDeviceID(gpu.Index, i))
		} else {
			ids = append(ids, deviceID(gpu.Index, i))
		}
	}
	return ids
}

// deviceID constructs the canonical device identifier used as both the
// K8s resource device ID and the KRAKEN tenant_id (fixed-slice mode).
func deviceID(gpuIdx, sliceIdx int) string {
	return fmt.Sprintf("keti-gpu%d-slice%d", gpuIdx, sliceIdx)
}

// lsuDeviceID constructs the device identifier for one LSU of a GPU.
func lsuDeviceID(gpuIdx, lsuIdx int) string {
	return fmt.Sprintf("keti-gpu%d-lsu%d", gpuIdx, lsuIdx)
}

// parseDeviceID extracts gpuIdx and sliceIdx from a device ID string.
func parseDeviceID(id string) (gpuIdx, sliceIdx int, err error) {
	_, err = fmt.Sscanf(id, "keti-gpu%d-slice%d", &gpuIdx, &sliceIdx)
	return
}

// parseLSUDeviceID extracts gpuIdx and lsuIdx from an LSU device ID string.
func parseLSUDeviceID(id string) (gpuIdx, lsuIdx int, err error) {
	_, err = fmt.Sscanf(id, "keti-gpu%d-lsu%d", &gpuIdx, &lsuIdx)
	return
}

// gpuIdxOf returns the physical GPU index that a device ID belongs to,
// handling both the slice and LSU naming schemes.
func gpuIdxOf(id string) (int, error) {
	if g, _, err := parseLSUDeviceID(id); err == nil {
		return g, nil
	}
	g, _, err := parseDeviceID(id)
	return g, err
}

// sliceSpec computes the per-allocation virtual resource share for a request of
// `reqUnits` units on a GPU.  In LSU mode `reqUnits` is the requested LSU count
// and the GPU's total units is gpu.LSU; otherwise it is one slice out of
// slicesPerGPU.  Returns virtual SM count, virtual memory (MB) and MPS percent.
func (p *DevicePlugin) sliceSpec(gpu GPUInfo, reqUnits int) (virtualSM int32, virtualMemMB int32, mpsPct int) {
	// ★이중제수 (Exp_73 §Part A): 컴퓨트(pct·SM)는 물리 LSU 로, 메모리는 광고 LSU 로.
	phys := p.physicalUnits(gpu) // 컴퓨트 100% 기준 (물리)
	adv := p.unitsPerGPU(gpu)    // 메모리 안전 기준 (광고 = 물리×factor)
	if phys <= 0 {
		phys = 1
	}
	if adv <= 0 {
		adv = 1
	}
	// 컴퓨트: 물리 기준 비례. ΣreqUnits 는 광고 LSU(=물리×factor)까지 admit 되므로
	// Σpct 는 100×factor% 까지 오른다 = 오버커밋의 실체. ★버그 아님 — Σ>100% 정상
	// (누가 클램프하면 오버커밋이 사라진다, Exp_73 §Part A). 단 개별 테넌트의 pct 는
	// MPS 유효 범위(1~100)로 클램프한다(단일 클라이언트가 >100% 는 무의미).
	virtualSM = int32(int64(gpu.SmCount) * int64(reqUnits) / int64(phys))
	mpsPct = 100 * reqUnits / phys
	if mpsPct > 100 {
		mpsPct = 100
	}
	if mpsPct < 1 {
		mpsPct = 1
	}
	// 메모리: 광고 기준 비례 → Σ(모든 테넌트) ≤ 물리 메모리 항상 성립(초과 불가).
	virtualMemMB = int32(gpu.MemoryMB * int64(reqUnits) / int64(adv))
	return virtualSM, virtualMemMB, mpsPct
}

// Start opens the Unix socket, starts the gRPC server, then registers with kubelet.
func (p *DevicePlugin) Start() error {
	// Remove stale socket from previous runs.
	_ = os.Remove(p.socketPath)

	ln, err := net.Listen("unix", p.socketPath)
	if err != nil {
		return fmt.Errorf("소켓 생성 실패 (%s): %w", p.socketPath, err)
	}

	p.server = grpc.NewServer()
	pluginapi.RegisterDevicePluginServer(p.server, p)

	p.wg.Add(1)
	go func() {
		defer p.wg.Done()
		if err := p.server.Serve(ln); err != nil {
			select {
			case <-p.stopCh:
			default:
				log.Printf("[plugin] gRPC 서버 오류: %v", err)
			}
		}
	}()

	// Wait briefly for the server to start accepting connections.
	if err := waitForSocket(p.socketPath, 5*time.Second); err != nil {
		p.server.Stop()
		return err
	}

	if err := p.registerWithKubelet(); err != nil {
		p.server.Stop()
		return fmt.Errorf("kubelet 등록 실패: %w", err)
	}

	log.Printf("[plugin] 준비 완료: resource=%s 슬라이스=%d개",
		p.resourceName, len(p.devices))
	return nil
}

// Stop shuts down the gRPC server and removes the socket.
func (p *DevicePlugin) Stop() {
	close(p.stopCh)
	p.server.Stop()
	p.wg.Wait()
	_ = os.Remove(p.socketPath)
}

// registerWithKubelet dials the kubelet socket and sends the Registration RPC.
func (p *DevicePlugin) registerWithKubelet() error {
	conn, err := grpc.NewClient(
		"unix://"+kubeletSocket,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return fmt.Errorf("kubelet 소켓 연결 실패: %w", err)
	}
	defer conn.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	_, err = pluginapi.NewRegistrationClient(conn).Register(ctx, &pluginapi.RegisterRequest{
		Version:      pluginapi.Version,
		Endpoint:     pluginSocketName,
		ResourceName: p.resourceName,
	})
	return err
}

// ── gRPC 메서드 ───────────────────────────────────────────────────────────────

func (p *DevicePlugin) GetDevicePluginOptions(
	_ context.Context, _ *pluginapi.Empty,
) (*pluginapi.DevicePluginOptions, error) {
	return &pluginapi.DevicePluginOptions{
		PreStartRequired:                false,
		GetPreferredAllocationAvailable: false,
	}, nil
}

// ListAndWatch streams the device list to kubelet.  It sends the full list on
// start and again whenever a health-check tick detects a change.
func (p *DevicePlugin) ListAndWatch(
	_ *pluginapi.Empty,
	stream pluginapi.DevicePlugin_ListAndWatchServer,
) error {
	if err := stream.Send(p.buildListResponse()); err != nil {
		return err
	}

	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-p.stopCh:
			return nil
		case <-stream.Context().Done():
			return nil
		case <-ticker.C:
			changed := p.refreshHealth()
			if changed {
				if err := stream.Send(p.buildListResponse()); err != nil {
					return err
				}
			}
		case <-p.updateCh:
			if err := stream.Send(p.buildListResponse()); err != nil {
				return err
			}
		}
	}
}

func (p *DevicePlugin) buildListResponse() *pluginapi.ListAndWatchResponse {
	p.devicesMu.RLock()
	defer p.devicesMu.RUnlock()

	resp := &pluginapi.ListAndWatchResponse{}
	for _, d := range p.devices {
		resp.Devices = append(resp.Devices, &pluginapi.Device{
			ID:     d.ID,
			Health: d.Health,
		})
	}
	return resp
}

// refreshHealth checks each GPU and updates device health.
// Returns true when any device health state changed.
func (p *DevicePlugin) refreshHealth() bool {
	p.devicesMu.Lock()
	defer p.devicesMu.Unlock()

	changed := false
	for _, gpu := range p.gpus {
		want := pluginapi.Healthy
		if !IsGPUHealthy(gpu.Index) {
			want = pluginapi.Unhealthy
		}
		for _, id := range p.deviceIDsForGPU(gpu) {
			if d, ok := p.devices[id]; ok && d.Health != want {
				d.Health = want
				changed = true
				log.Printf("[plugin] GPU%d health 변경: %s", gpu.Index, want)
			}
		}
	}
	return changed
}

// IsRegisteredDevice reports whether devID is a canonical(등록에 실제 사용된)
// device ID. Watcher 가 deregister 대상 선별에 사용 (Exp_41: LSU 모드에서
// 파드당 N개 ID 중 1개만 등록되므로, 나머지 N-1개 시도는 노이즈였음).
func (p *DevicePlugin) IsRegisteredDevice(devID string) bool {
	_, ok := p.allocations.Load(devID)
	return ok
}

// AllocatedOlderThan returns canonical device IDs allocated more than d ago.
//
// [Exp_97 Part D] ★유예가 필수다. podToDevices 는 kubelet 체크포인트를 15초 주기로 읽으므로
// 방금 할당된 파드는 아직 그 맵에 없다. 유예 없이 대조하면 **살아있는 슬롯을 고아로 오판해
// 회수**한다(실측: 할당 2초 만에 회수돼 axis 시간축·relaxed 축이 FAIL 했다).
// 유예는 체크포인트 주기(15s)보다 넉넉히 크게 잡는다.
func (p *DevicePlugin) AllocatedOlderThan(d time.Duration) []string {
	var out []string
	cut := time.Now().Add(-d)
	p.allocations.Range(func(k, v interface{}) bool {
		s, ok1 := k.(string)
		r, ok2 := v.(allocationRecord)
		if ok1 && ok2 && r.at.Before(cut) {
			out = append(out, s)
		}
		return true
	})
	return out
}

// RestoreAllocations reseeds p.allocations from the kubelet checkpoint after a
// plugin restart. [Exp_140 5-A 실측 결함 수정]
//
// ★없으면: 재기동 후 RequestedUnits 가 전부 (0,false) → bless_feeder 의 scan 이
//
//	라이브 소켓을 stale 로 배제 → feeder 재등록 0건 → 이미 armed(time_mode=1)인
//	파드는 time_add 공급이 끊겨 커널 게이트에서 **영구 동결**된다
//	(실측: e140-victim kernels=54 정지. 파드는 Running 이라 아무 경보도 없다 —
//	조용한 사고, T-5. entrypoint 원자 교체(1-A)로 안 죽게 된 파드가 이번엔 굶었다).
//	NPU 모델 슬롯의 RestoreFromCheckpoint(Exp_53)와 같은 계열의 복원이다.
//
// canonical devID = Allocate 가 소켓 디렉터리를 마운트한 ID(= controller tenant_id).
//
//	체크포인트는 ID 순서를 보장하지 않고, 소켓 **디렉터리**는 지난 할당들의 잔재로
//	거의 전부 실존한다(실측: 178개 전부) — 디렉터리 유무로는 못 고른다.
//	★라이브 bless-*.sock 이 있는 ID(findBlessSock, 생사 확인 포함)가 canonical 이다.
//	소켓이 아직 없으면(libbless init 전) 복원 보류 — 그 상태로는 와이어러도 어차피
//	배선할 수 없고, init 되면 다음 복원 기회가 없으므로 경고를 남긴다.
//	tenantIdx 는 재기동 후 알 수 없어 -1 로 둔다(그 값의 소비처는 Allocate 시
//	env 주입뿐이라 복원 경로에서는 쓰이지 않는다).
func (p *DevicePlugin) RestoreAllocations(checkpointPath, resourceName, sockDir string) int {
	if sockDir == "" {
		return 0
	}
	raw, err := os.ReadFile(checkpointPath)
	if err != nil {
		log.Printf("[plugin] 할당 복원 생략: checkpoint 읽기 실패 (%v)", err)
		return 0
	}
	var cp checkpointData
	if err := json.Unmarshal(raw, &cp); err != nil {
		log.Printf("[plugin] 할당 복원 생략: checkpoint 파싱 실패 (%v)", err)
		return 0
	}
	n := 0
	for _, entry := range cp.Data.PodDeviceEntries {
		if entry.ResourceName != resourceName {
			continue
		}
		ids := entry.allDeviceIDs()
		if len(ids) == 0 {
			continue
		}
		canonical := ""
		for _, id := range ids {
			if findBlessSock(filepath.Join(sockDir, id)) != "" {
				canonical = id
				break
			}
		}
		if canonical == "" {
			// libbless 가 아직 init 전이었거나 이미 정리된 파드 —
			// 조용히 넘기지 않는다(T-5).
			log.Printf("[plugin] 할당 복원 보류: pod=%s units=%d — 라이브 bless 소켓 미발견",
				entry.PodUID, len(ids))
			continue
		}
		if _, exists := p.allocations.Load(canonical); exists {
			continue
		}
		p.allocations.Store(canonical,
			allocationRecord{tenantIdx: -1, units: len(ids), at: time.Now()})
		log.Printf("[plugin] 할당 복원: device=%s units=%d (pod=%s, checkpoint)",
			canonical, len(ids), entry.PodUID)
		n++
	}
	return n
}

// ForgetDevice drops the local allocation record after a successful deregister.
func (p *DevicePlugin) ForgetDevice(devID string) {
	p.allocations.Delete(devID)
}

// RequestedUnits returns the requested LSU count for an active allocation
// (Exp_74 — 시간축 auto-arm ratio). 0,false = 할당 없음(=stale). bless_feeder
// 가 이걸로 (a) stale 배제 (b) LSU 비례 ratio 를 얻는다.
func (p *DevicePlugin) RequestedUnits(devID string) (int, bool) {
	v, ok := p.allocations.Load(devID)
	if !ok {
		return 0, false
	}
	return v.(allocationRecord).units, true
}

// Allocate is called by kubelet when a pod requests keti.re.kr/gpu-slice.
//
// For each container request the method:
//  1. Parses the device ID to determine the physical GPU index.
//  2. Computes the per-slice virtual SM count and memory quota.
//  3. Calls the KRAKEN controller POST /register to create the SHM alloc slot.
//  4. Returns env vars and an SHM mount so the container's gating_lib.so can
//     open the shared state without any additional plumbing.
func (p *DevicePlugin) Allocate(
	_ context.Context,
	reqs *pluginapi.AllocateRequest,
) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}

	for _, containerReq := range reqs.ContainerRequests {
		if len(containerReq.DevicesIds) == 0 {
			return nil, fmt.Errorf("빈 device 요청")
		}

		// In LSU mode kubelet passes one device ID per requested LSU; all must
		// belong to the same physical GPU.  In slice mode there is one ID.
		// reqUnits = number of logical units requested for this container.
		reqUnits := len(containerReq.DevicesIds)
		devID := containerReq.DevicesIds[0]
		gpuIdx, err := gpuIdxOf(devID)
		if err != nil {
			return nil, fmt.Errorf("device ID 파싱 실패 (%s): %w", devID, err)
		}
		if p.lsuMode {
			for _, d := range containerReq.DevicesIds {
				g, e := gpuIdxOf(d)
				if e != nil || g != gpuIdx {
					return nil, fmt.Errorf("LSU 요청이 여러 GPU에 걸침 (%v)", containerReq.DevicesIds)
				}
			}
		} else {
			reqUnits = 1 // slice mode: one slice per container
		}

		gpu, err := p.findGPU(gpuIdx)
		if err != nil {
			return nil, err
		}
		if gpu.MIGCapable && !IsGPUHealthy(gpu.Index) {
			return nil, fmt.Errorf("full GPU allocation unavailable: MIG mode or device state changed")
		}

		// Proportional resource share for the requested units.
		virtualSM, virtualMemMB, mpsPct := p.sliceSpec(gpu, reqUnits)

		// Register with the KRAKEN controller.
		// tenant_id is the first device ID — deterministic and unique.
		tenantIdx, err := p.controller.Register(devID, virtualSM, virtualMemMB, 1.0)
		if err != nil {
			return nil, fmt.Errorf("controller 등록 실패 (device=%s): %w", devID, err)
		}

		log.Printf("[plugin] Allocate: device=%s gpu=%d units=%d sm=%d mem=%dMB mps=%d%% slot=%d",
			devID, gpuIdx, reqUnits, virtualSM, virtualMemMB, mpsPct, tenantIdx)

		p.allocations.Store(devID, allocationRecord{tenantIdx: tenantIdx, units: reqUnits, at: time.Now()})

		// Path of the KRAKEN shared-memory file on the host.
		// The container gets it bind-mounted at the same path.
		shmPath := fmt.Sprintf("%s/kraken_%s", shmBasePath, krakenGroupID)

		car := &pluginapi.ContainerAllocateResponse{
			Envs: map[string]string{
				// Read by gating_lib.so kraken_init() constructor.
				"KRAKEN_TENANT": devID,
				"KRAKEN_GROUP":  krakenGroupID,
				// Read by CUDA MPS daemon to cap SM usage per process.
				"CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": fmt.Sprintf("%d", mpsPct),
				// Pin the container to the correct physical GPU.
				"CUDA_VISIBLE_DEVICES": fmt.Sprintf("%d", gpuIdx),
				// Informational: lets workload code log its allocation.
				"KRAKEN_TENANT_IDX":     fmt.Sprintf("%d", tenantIdx),
				"KRAKEN_VIRTUAL_SM":     fmt.Sprintf("%d", virtualSM),
				"KRAKEN_VIRTUAL_MEM_MB": fmt.Sprintf("%d", virtualMemMB),
			},
			Mounts: []*pluginapi.Mount{
				{
					// Expose the SHM file so gating_lib.so can call shm_open().
					HostPath:      shmPath,
					ContainerPath: shmPath,
					ReadOnly:      false,
				},
			},
		}

		// GPU 디바이스 부여 (Exp_71 Part A — CDI 채택안):
		// containerd(enable_cdi)가 /var/run/cdi/nvidia.yaml 스펙으로 디바이스
		// 노드 + 드라이버 라이브러리 + ldcache 훅을 주입한다. runtimeClass·
		// NVIDIA_VISIBLE_DEVICES 수동 지정 불요. UUID 한정이라 다중 GPU 에서도
		// 해당 GPU 만 노출된다 (NPU 블록 부여와 대칭 — 경계는 kubelet 응답으로).
		if gpu.UUID != "" {
			car.CdiDevices = []*pluginapi.CDIDevice{
				{Name: fmt.Sprintf("nvidia.com/gpu=%s", gpu.UUID)},
			}
		}

		// MPS 모드 (Exp_71 Part B — 독립 롤백: --mps-pipe-dir 제거로 해제):
		if p.mpsPipeDir != "" {
			car.Envs["CUDA_MPS_PIPE_DIRECTORY"] = p.mpsPipeDir
			car.Envs["BLESS_SPACE_MODE"] = "mps_env"
			car.Mounts = append(car.Mounts, &pluginapi.Mount{
				HostPath: p.mpsPipeDir, ContainerPath: p.mpsPipeDir, ReadOnly: false,
			})
		}

		// libbless LD_PRELOAD 주입 (Exp_33). BLESS_LIMIT_PCT 는 0<v<100 만
		// 유효(v=100 도 암묵 50% 캡으로 떨어짐 — Exp_23 §2.3)이므로 99 로 클램프.
		if p.blessSO != "" {
			pct := mpsPct
			if pct >= 100 {
				pct = 99
			}
			if pct < 1 {
				pct = 1
			}
			car.Envs["LD_PRELOAD"] = p.blessSO
			car.Envs["BLESS_TENANT"] = devID
			car.Envs["BLESS_LIMIT_PCT"] = fmt.Sprintf("%d", pct)
			// mem quota 는 libbless 소켓 명령(mem_quota)으로 집행 — env 는 표시용.
			car.Envs["BLESS_MEM_QUOTA_MB"] = fmt.Sprintf("%d", virtualMemMB)
			car.Mounts = append(car.Mounts, &pluginapi.Mount{
				HostPath: p.blessSO, ContainerPath: p.blessSO, ReadOnly: true,
			})
			if p.blessSockDir != "" {
				// 테넌트별 하위 디렉토리를 컨테이너 /tmp 로 — pid 네임스페이스가
				// 달라도 소켓 파일이 호스트에서 tenant 별로 식별된다.
				hostDir := filepath.Join(p.blessSockDir, devID)
				_ = os.MkdirAll(hostDir, 0o777)
				_ = os.Chmod(hostDir, 0o777)
				car.Mounts = append(car.Mounts, &pluginapi.Mount{
					HostPath: hostDir, ContainerPath: "/tmp", ReadOnly: false,
				})
				// [Exp_73] time_stats 파일 발화 → feeder occupancy. 컨테이너
				// /tmp/feed.log = 호스트 <blessSockDir>/<devID>/feed.log
				// (bless_feeder.go 가 등록하는 log 경로와 일치).
				car.Envs["BLESS_STATS_LOG"] = "/tmp/feed.log"
				// [Exp_75] 워크로드 클래스 선언(파드 env KRAKEN_WORKLOAD_CLASS) →
				// /tmp/class = 호스트 <blessSockDir>/<devID>/class. wirer 쌍 판정용.
				car.Envs["BLESS_CLASS_LOG"] = "/tmp/class"
			}
		}

		// [Exp_131] CUPTI 커널 관측 주입 (--tracer-so + --cupti-dir 지정 시만).
		//   libbless 와 같은 hostPath 방식. HostPath==ContainerPath 라 컨테이너
		//   내부 경로를 하드코딩하지 않는다(libbless 주입과 동일 규약).
		//
		//   LD_PRELOAD 순서 = libbless → libcupti → tracer (Exp_131 2-A 실측 근거).
		//   libbless 를 앞에 두는 이유: 기존 경로의 심볼 우선순위를 바꾸지 않는다
		//   (tracer 는 심볼 인터포지션이 아니라 CUPTI 콜백이라 순서 무관하지만,
		//   무관함을 근거로 **기존 항목을 뒤로 밀지 않는다**).
		//
		//   ⚠️ tracer 를 LD_PRELOAD 로 걸면 컨테이너의 모든 프로세스에 적재된다.
		//      Exp_131 1-A: `ldd`(로더 추적 모드)는 이 상태에서 행(hang)한다.
		//      워크로드 경로(sh/python/CUDA)는 정상이나, 진단용 ldd 는 쓸 수 없다.
		if p.tracerSO != "" && p.cuptiDir != "" {
			cupti := filepath.Join(p.cuptiDir, "libcupti.so.12")
			pre := car.Envs["LD_PRELOAD"]
			if pre != "" {
				pre += ":"
			}
			car.Envs["LD_PRELOAD"] = pre + cupti + ":" + p.tracerSO
			car.Mounts = append(car.Mounts,
				&pluginapi.Mount{HostPath: p.tracerSO, ContainerPath: p.tracerSO, ReadOnly: true},
				&pluginapi.Mount{HostPath: p.cuptiDir, ContainerPath: p.cuptiDir, ReadOnly: true})
			// 수집 파일은 libbless 소켓 디렉토리(컨테이너 /tmp)와 같은 자리로 —
			// 호스트에서 <blessSockDir>/<devID>/trace.txt 로 읽힌다.
			// blessSockDir 이 비면 갈 곳이 없다 → main.go 가 기동 시 거부한다(T-5).
			// ★"%p" = pid 치환(Exp_131). LD_PRELOAD 는 컨테이너의 모든
			//   프로세스에 걸리고 tracer 는 KTRACE_OUT 을 "w" 로 연다 —
			//   같은 경로면 마지막 프로세스(예: 파드 command 의 `cat`)가
			//   수집분을 길이 0 으로 잘라낸다. 프로세스마다 파일을 분리한다.
			car.Envs["KTRACE_OUT"] = "/tmp/trace.%p.txt"
			car.Envs["KTRACE_FLUSH_MS"] = fmt.Sprintf("%d", p.tracerFlushMS)
			car.Envs["KTRACE_BUF_KB"] = fmt.Sprintf("%d", p.tracerBufKB)
			car.Envs["KRAKEN_TRACE"] = "1"
		}

		// [Exp_134 3-B] 종료 감지 자동 등록 — KRAKEN_BOOSTER=1 일 때만.
		//   지금까지 Booster 는 수동 POST 로만 등록됐고 그래서 actions=0 이었다
		//   (Exp_126 §4-3). 파드 생명주기에 엮는다: 여기서 등록, watcher 의
		//   deregister 경로에서 해제.
		//   ★pid 는 보내지 않는다(0) — Allocate 시점엔 워크로드가 없고, 소켓명의
		//     pid 는 컨테이너 pid 이며, MPS 하 nvidia-smi 의 pid 는 MPS 서버다.
		//     대신 libbless 제어 소켓 glob 을 생존 신호로 준다.
		if p.boosterRegister && p.controller != nil && gpu.UUID != "" {
			alive := ""
			if p.blessSockDir != "" {
				alive = filepath.Join(p.blessSockDir, devID, "bless-*.sock")
			}
			if err := p.controller.BoosterRegister(devID, gpu.UUID, alive); err != nil {
				// [T-5] 조용히 넘어가지 않는다. 등록 실패 = 그 테넌트만 감지 대상
				// 밖이 되는 것이라 Allocate 자체는 진행한다.
				log.Printf("[plugin][경고] Booster 등록 실패 tenant=%s: %v — "+
					"이 테넌트는 종료 감지 대상에서 빠진다", devID, err)
			}
		}

		// [Exp_134] 빠른 종료 주입 (--fast-teardown-dir 지정 시만).
		//   파이썬은 기동 시 sys.path 의 sitecustomize 를 자동 import 한다.
		//   그 디렉터리를 PYTHONPATH 로 얹으면 워크로드를 한 줄도 고치지 않고
		//   종료 경로를 바꿀 수 있다(Exp_96). 지금까지 이 주입은 **측정 하네스에만**
		//   있었고 라이브 경로에는 없었다(Exp_133 §2-A) — 그것을 여기로 옮긴다.
		//   HostPath == ContainerPath 규약은 libbless·tracer 와 같다.
		//
		//   ⚠️ PYTHONPATH 는 **덮어쓰기**다. Allocate 는 파드의 기존 env 를 읽을 수
		//      없으므로, 파드가 PYTHONPATH 를 직접 쓰고 있으면 충돌한다.
		//      libcupti 를 LD_PRELOAD 로 실어 LD_LIBRARY_PATH 를 피한 것(Exp_131)과
		//      달리 파이썬에는 대체 경로가 없다. 병합 순서는 §2-A 에서 실측했다.
		if p.fastTeardownDir != "" {
			car.Envs["PYTHONPATH"] = p.fastTeardownDir
			car.Envs["BLESS_FAST_TEARDOWN"] = "1"
			car.Mounts = append(car.Mounts, &pluginapi.Mount{
				HostPath: p.fastTeardownDir, ContainerPath: p.fastTeardownDir,
				ReadOnly: true,
			})
		}

		resp.ContainerResponses = append(resp.ContainerResponses, car)
	}

	return resp, nil
}

func (p *DevicePlugin) GetPreferredAllocation(
	_ context.Context,
	_ *pluginapi.PreferredAllocationRequest,
) (*pluginapi.PreferredAllocationResponse, error) {
	return &pluginapi.PreferredAllocationResponse{}, nil
}

func (p *DevicePlugin) PreStartContainer(
	_ context.Context,
	_ *pluginapi.PreStartContainerRequest,
) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}

// DryRun prints exactly what this plugin would advertise to kubelet and how a
// sample Allocate would map a request — without opening the kubelet socket,
// without registering with kubelet, and without calling the controller (so it
// mutates no shared cluster/controller state).  Used for local verification on
// a shared cluster where deploying the plugin is not desired.
func (p *DevicePlugin) DryRun(sampleReqUnits int) {
	log.Printf("[dry-run] ===== LSU 노출 검증 (DRY-RUN, 클러스터 무변경) =====")
	log.Printf("[dry-run] mode=%s  resource=%s",
		map[bool]string{true: "LSU", false: "slice"}[p.lsuMode], p.resourceName)

	total := 0
	for _, gpu := range p.gpus {
		n := p.unitsPerGPU(gpu)
		total += n
		if p.lsuMode {
			log.Printf("[dry-run] GPU%d %s: LSU=%d (%s)  SM=%d mem=%dMB → 광고 device %d개",
				gpu.Index, gpu.Name, gpu.LSU, gpu.LSUSource, gpu.SmCount, gpu.MemoryMB, n)
		} else {
			log.Printf("[dry-run] GPU%d %s: slices=%d  SM=%d mem=%dMB → 광고 device %d개",
				gpu.Index, gpu.Name, n, gpu.SmCount, gpu.MemoryMB, n)
		}
	}
	log.Printf("[dry-run] 노드 전체 광고 수량: %s = %d", p.resourceName, total)

	// ListAndWatch 응답 미리보기 (앞 8개만)
	resp := p.buildListResponse()
	log.Printf("[dry-run] ListAndWatch: 총 %d개 device, Health=Healthy. 예시 ID:", len(resp.Devices))
	for i, d := range resp.Devices {
		if i >= 8 {
			log.Printf("[dry-run]   ... (+%d more)", len(resp.Devices)-8)
			break
		}
		log.Printf("[dry-run]   - %s", d.ID)
	}

	// Allocate 시뮬레이션 (controller.Register 호출 안 함)
	if len(p.gpus) == 0 {
		return
	}
	gpu := p.gpus[0]
	reqUnits := sampleReqUnits
	if !p.lsuMode {
		reqUnits = 1
	}
	if reqUnits > p.unitsPerGPU(gpu) {
		reqUnits = p.unitsPerGPU(gpu)
	}
	virtualSM, virtualMemMB, mpsPct := p.sliceSpec(gpu, reqUnits)
	var reqIDs []string
	for _, id := range p.deviceIDsForGPU(gpu) {
		if len(reqIDs) >= reqUnits {
			break
		}
		reqIDs = append(reqIDs, id)
	}
	log.Printf("[dry-run] --- Allocate 시뮬레이션: GPU%d 에 %s=%d 요청 ---",
		gpu.Index, p.resourceName, reqUnits)
	log.Printf("[dry-run]   요청 device IDs: %v", reqIDs)
	log.Printf("[dry-run]   → controller POST /register {tenant_id=%s, virtual_sm=%d, virtual_mem_mb=%d, weight=1.0}",
		reqIDs[0], virtualSM, virtualMemMB)
	log.Printf("[dry-run]   → 컨테이너 주입 Env:")
	log.Printf("[dry-run]       KRAKEN_TENANT=%s  KRAKEN_GROUP=%s", reqIDs[0], krakenGroupID)
	log.Printf("[dry-run]       CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=%d  CUDA_VISIBLE_DEVICES=%d", mpsPct, gpu.Index)
	log.Printf("[dry-run]       KRAKEN_VIRTUAL_SM=%d  KRAKEN_VIRTUAL_MEM_MB=%d", virtualSM, virtualMemMB)
	log.Printf("[dry-run]   → 컨테이너 Mount: %s/kraken_%s (rw)", shmBasePath, krakenGroupID)
	log.Printf("[dry-run] ===== 검증 종료 (kubelet 등록·controller 변경 없음) =====")
}

// ── helpers ───────────────────────────────────────────────────────────────────

func (p *DevicePlugin) findGPU(idx int) (GPUInfo, error) {
	for _, g := range p.gpus {
		if g.Index == idx {
			return g, nil
		}
	}
	return GPUInfo{}, fmt.Errorf("GPU%d 정보 없음", idx)
}

// waitForSocket polls until the Unix socket path exists or the deadline passes.
func waitForSocket(path string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("소켓 타임아웃: %s", path)
}
