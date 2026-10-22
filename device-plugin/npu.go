package main

// npu.go — Furiosa Warboy PE 단위 Device Plugin (Exp_33 Phase 1, 설계: Exp_32 §6 옵션 A).
//
// 광고: keti.io/npu = PE 단위 device (Warboy 1기 = pe0 + pe1 = 2 devices).
//   리소스 이름은 기존 keti.io/npu 를 승계한다 (신설 금지 — 삼중광고 방지, Exp_31).
// Allocate 부여 세트는 furiosa 공식 플러그인 실측(Exp_32 §2)을 참조 모델로 한다:
//   PE 노드만이 아니라 npu{i}_mgmt + npu{i}ch0..3 을 동반 부여해야 SDK 가 구동된다.
// fusion(2-PE 융합, npu0pe0-1)은 all-or-nothing 이므로 (NPU 트랙 확정지식)
//   한 컨테이너가 PE 2개를 동시 요청하면 fusion 노드를 부여한다.
//   (파드 annotation 힌트는 Allocate API 에 전달되지 않아 v1 은 수량 규약만 사용
//    — 편차 기록: Exp_33 report §Phase1)

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

const (
	npuSocketName   = "kraken-npu.sock"
	npuResourceName = "keti.io/npu"
	// maxNPUScan bounds discovery of /dev/npu{i}pe{j} nodes.
	maxNPUScan = 8
	maxPEScan  = 8
)

// NPUDevice is one advertised PE.
type NPUDevice struct {
	NpuIdx  int
	PEIdx   int
	DevPath string // /dev/npu0pe0
}

// npuDeviceID builds the K8s device ID for one PE.
func npuDeviceID(npuIdx, peIdx int) string {
	return fmt.Sprintf("keti-npu%d-pe%d", npuIdx, peIdx)
}

// parseNPUDeviceID extracts npuIdx/peIdx from a device ID.
func parseNPUDeviceID(id string) (npuIdx, peIdx int, err error) {
	_, err = fmt.Sscanf(id, "keti-npu%d-pe%d", &npuIdx, &peIdx)
	return npuIdx, peIdx, err
}

// DiscoverNPUPEs scans devRoot for PE character devices (npu{i}pe{j}).
// fusion 노드(npu0pe0-1)와 mgmt/ch 노드는 광고 대상이 아니라 Allocate 시 동반 부여.
func DiscoverNPUPEs(devRoot string) ([]NPUDevice, error) {
	var out []NPUDevice
	for npu := 0; npu < maxNPUScan; npu++ {
		for pe := 0; pe < maxPEScan; pe++ {
			p := filepath.Join(devRoot, fmt.Sprintf("npu%dpe%d", npu, pe))
			if _, err := os.Stat(p); err == nil {
				out = append(out, NPUDevice{NpuIdx: npu, PEIdx: pe, DevPath: p})
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("PE 디바이스 없음 (%s/npu*pe*)", devRoot)
	}
	return out, nil
}

// npuCompanions returns the mgmt/ch device paths that must accompany any PE
// grant (furiosa 공식 세트 실측 대응 — Exp_32 §2). mgmt 는 필수, ch 는 존재하는
// 것만 포함.
func npuCompanions(devRoot string, npuIdx int) ([]string, error) {
	mgmt := filepath.Join(devRoot, fmt.Sprintf("npu%d_mgmt", npuIdx))
	if _, err := os.Stat(mgmt); err != nil {
		return nil, fmt.Errorf("mgmt 노드 없음 (%s) — SDK 구동 불가 세트", mgmt)
	}
	out := []string{mgmt}
	for ch := 0; ch < 4; ch++ {
		p := filepath.Join(devRoot, fmt.Sprintf("npu%dch%d", npuIdx, ch))
		if _, err := os.Stat(p); err == nil {
			out = append(out, p)
		}
	}
	return out, nil
}

// NPUPlugin implements the device-plugin API for keti.io/npu.
type NPUPlugin struct {
	pluginapi.UnimplementedDevicePluginServer

	devRoot    string // "/dev" (유닛 테스트에서 교체)
	socketPath string
	devices    map[string]NPUDevice

	server *grpc.Server
	stopCh chan struct{}
	wg     sync.WaitGroup
}

// ErrNoDirectPEs signals that every PE is proxy-owned (Exp_59 다중 모델) —
// 구성 오류가 아니라 직접 경로(keti.io/npu) 없음 상태.
var ErrNoDirectPEs = errors.New("직접 할당 풀이 비었음 (전 PE 가 프록시 전용)")

// NewNPUPlugin discovers PEs under devRoot and prepares the plugin.
// excludePE (예: "npu0pe0")는 직접 할당 풀에서 제외한다 — Exp_47 게이트 1(B):
// 프록시 전용 PE 는 모델별 리소스(npu_model.go)로만 광고해 이중계상을 원천
// 차단한다 (같은 PE 가 두 리소스 이름으로 계상되면 물리 초과 배치 — Exp_31 재발 금지).
func NewNPUPlugin(devRoot string, excludePEs ...string) (*NPUPlugin, error) {
	excluded := make(map[string]bool, len(excludePEs))
	for _, pe := range excludePEs {
		if pe != "" {
			excluded[pe] = true
		}
	}
	pes, err := DiscoverNPUPEs(devRoot)
	if err != nil {
		return nil, err
	}
	p := &NPUPlugin{
		devRoot:    devRoot,
		socketPath: filepath.Join(pluginSocketDir, npuSocketName),
		devices:    make(map[string]NPUDevice),
		stopCh:     make(chan struct{}),
	}
	for _, d := range pes {
		name := fmt.Sprintf("npu%dpe%d", d.NpuIdx, d.PEIdx)
		if excluded[name] {
			log.Printf("[npu] 제외(프록시 전용 PE): %s — 모델별 리소스로만 광고", name)
			continue
		}
		p.devices[npuDeviceID(d.NpuIdx, d.PEIdx)] = d
		log.Printf("[npu] 발견: %s (%s)", npuDeviceID(d.NpuIdx, d.PEIdx), d.DevPath)
	}
	if len(p.devices) == 0 {
		// Exp_59: 다중 모델 구성에서는 전 PE 가 프록시 소유일 수 있다 —
		// 오류가 아니라 "직접 경로 없음" 상태이므로 센티넬로 구분한다
		// (호출측이 keti.io/npu 광고를 생략).
		return nil, ErrNoDirectPEs
	}
	return p, nil
}

// Start serves the gRPC endpoint and registers keti.io/npu with kubelet.
func (p *NPUPlugin) Start() error {
	_ = os.Remove(p.socketPath)
	ln, err := net.Listen("unix", p.socketPath)
	if err != nil {
		return fmt.Errorf("NPU 소켓 생성 실패 (%s): %w", p.socketPath, err)
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
				log.Printf("[npu] gRPC 서버 오류: %v", err)
			}
		}
	}()

	if err := waitForSocket(p.socketPath, 5*time.Second); err != nil {
		p.server.Stop()
		return err
	}

	conn, err := grpc.NewClient(
		"unix://"+kubeletSocket,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		p.server.Stop()
		return fmt.Errorf("kubelet 소켓 연결 실패: %w", err)
	}
	defer conn.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err = pluginapi.NewRegistrationClient(conn).Register(ctx, &pluginapi.RegisterRequest{
		Version:      pluginapi.Version,
		Endpoint:     npuSocketName,
		ResourceName: npuResourceName,
	})
	if err != nil {
		p.server.Stop()
		return fmt.Errorf("kubelet 등록 실패 (npu): %w", err)
	}

	log.Printf("[npu] 준비 완료: resource=%s PE=%d개", npuResourceName, len(p.devices))
	return nil
}

// Stop shuts down the server and removes the socket.
func (p *NPUPlugin) Stop() {
	close(p.stopCh)
	if p.server != nil {
		p.server.Stop()
	}
	p.wg.Wait()
	_ = os.Remove(p.socketPath)
}

// ── gRPC 메서드 ───────────────────────────────────────────────────────────────

func (p *NPUPlugin) GetDevicePluginOptions(
	_ context.Context, _ *pluginapi.Empty,
) (*pluginapi.DevicePluginOptions, error) {
	return &pluginapi.DevicePluginOptions{
		PreStartRequired:                false,
		GetPreferredAllocationAvailable: true, // fusion 페어 선호를 위해 구현
	}, nil
}

func (p *NPUPlugin) ListAndWatch(
	_ *pluginapi.Empty,
	stream pluginapi.DevicePlugin_ListAndWatchServer,
) error {
	resp := &pluginapi.ListAndWatchResponse{}
	for id := range p.devices {
		resp.Devices = append(resp.Devices, &pluginapi.Device{
			ID: id, Health: pluginapi.Healthy,
		})
	}
	sort.Slice(resp.Devices, func(i, j int) bool {
		return resp.Devices[i].ID < resp.Devices[j].ID
	})
	if err := stream.Send(resp); err != nil {
		return err
	}
	// PE 노드는 정적 — health 는 노드 존재로 갈음 (30s 재확인).
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-p.stopCh:
			return nil
		case <-stream.Context().Done():
			return nil
		case <-ticker.C:
			changed := false
			for id, d := range p.devices {
				if _, err := os.Stat(d.DevPath); err != nil {
					log.Printf("[npu] 디바이스 소실: %s", id)
					changed = true
				}
			}
			if changed {
				if err := stream.Send(resp); err != nil {
					return err
				}
			}
		}
	}
}

// GetPreferredAllocation prefers the pe0+pe1 pair of one NPU for 2-unit
// requests so fusion grants stay on a single physical NPU.
func (p *NPUPlugin) GetPreferredAllocation(
	_ context.Context,
	req *pluginapi.PreferredAllocationRequest,
) (*pluginapi.PreferredAllocationResponse, error) {
	resp := &pluginapi.PreferredAllocationResponse{}
	for _, creq := range req.ContainerRequests {
		avail := append([]string(nil), creq.AvailableDeviceIDs...)
		sort.Strings(avail)
		want := int(creq.AllocationSize)
		pick := avail
		if want == 2 {
			// 같은 npu 의 pe 페어 우선
			byNpu := map[int][]string{}
			for _, id := range avail {
				if n, _, err := parseNPUDeviceID(id); err == nil {
					byNpu[n] = append(byNpu[n], id)
				}
			}
			for _, ids := range byNpu {
				if len(ids) >= 2 {
					pick = ids[:2]
					break
				}
			}
		}
		if want < len(pick) {
			pick = pick[:want]
		}
		resp.ContainerResponses = append(resp.ContainerResponses,
			&pluginapi.ContainerPreferredAllocationResponse{DeviceIDs: pick})
	}
	return resp, nil
}

// npuAllocateOne computes the device set + envs for one container request.
// 반환: DeviceSpec 경로 목록, envs. (테스트 가능하도록 분리)
func (p *NPUPlugin) npuAllocateOne(ids []string) ([]string, map[string]string, error) {
	if len(ids) == 0 {
		return nil, nil, fmt.Errorf("빈 device 요청")
	}
	if len(ids) > 2 {
		return nil, nil, fmt.Errorf("PE 요청은 1(단일) 또는 2(fusion)만 지원: %v", ids)
	}
	npuIdx, pe0, err := parseNPUDeviceID(ids[0])
	if err != nil {
		return nil, nil, fmt.Errorf("device ID 파싱 실패 (%s): %w", ids[0], err)
	}

	var paths []string
	envs := map[string]string{}

	if len(ids) == 1 {
		paths = append(paths, filepath.Join(p.devRoot,
			fmt.Sprintf("npu%dpe%d", npuIdx, pe0)))
		envs["KETI_NPU_PE"] = fmt.Sprintf("npu%dpe%d", npuIdx, pe0)
		envs["KETI_NPU_FUSION"] = "0"
		envs["NPU_VISIBLE_PE"] = fmt.Sprintf("%d", pe0)
	} else {
		n1, pe1, err := parseNPUDeviceID(ids[1])
		if err != nil {
			return nil, nil, fmt.Errorf("device ID 파싱 실패 (%s): %w", ids[1], err)
		}
		if n1 != npuIdx {
			return nil, nil, fmt.Errorf("fusion 요청이 여러 NPU에 걸침: %v", ids)
		}
		lo, hi := pe0, pe1
		if lo > hi {
			lo, hi = hi, lo
		}
		fusion := filepath.Join(p.devRoot,
			fmt.Sprintf("npu%dpe%d-%d", npuIdx, lo, hi))
		if _, err := os.Stat(fusion); err != nil {
			return nil, nil, fmt.Errorf("fusion 노드 없음 (%s)", fusion)
		}
		paths = append(paths, fusion)
		envs["KETI_NPU_PE"] = fmt.Sprintf("npu%dpe%d-%d", npuIdx, lo, hi)
		envs["KETI_NPU_FUSION"] = "1"
		envs["NPU_VISIBLE_PE"] = fmt.Sprintf("%d-%d", lo, hi)
	}

	comp, err := npuCompanions(p.devRoot, npuIdx)
	if err != nil {
		return nil, nil, err
	}
	paths = append(paths, comp...)
	envs["KETI_NPU_DEVICE_IDS"] = fmt.Sprintf("%v", ids)
	return paths, envs, nil
}

// Allocate grants PE(단일) 또는 fusion 노드 + mgmt/ch 동반 세트.
func (p *NPUPlugin) Allocate(
	_ context.Context,
	reqs *pluginapi.AllocateRequest,
) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}
	for _, creq := range reqs.ContainerRequests {
		paths, envs, err := p.npuAllocateOne(creq.DevicesIds)
		if err != nil {
			return nil, err
		}
		car := &pluginapi.ContainerAllocateResponse{Envs: envs}
		for _, hp := range paths {
			car.Devices = append(car.Devices, &pluginapi.DeviceSpec{
				HostPath:      hp,
				ContainerPath: hp,
				Permissions:   "rw",
			})
		}
		log.Printf("[npu] Allocate: ids=%v → devices=%v fusion=%s",
			creq.DevicesIds, paths, envs["KETI_NPU_FUSION"])
		resp.ContainerResponses = append(resp.ContainerResponses, car)
	}
	return resp, nil
}

func (p *NPUPlugin) PreStartContainer(
	_ context.Context,
	_ *pluginapi.PreStartContainerRequest,
) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}
