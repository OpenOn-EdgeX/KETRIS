package main

// npu_model.go — 모델별 NPU 리소스 (Exp_47, 게이트 1(B)+게이트 2(A)).
//
// 프록시 전용 PE(예: npu0pe0)를 `keti.io/npu-<model>` 로 광고한다. 프록시가 PE 의
// 유일 소유자(상시 기동)이므로 이 리소스의 소비 파드는 디바이스 노드를 받지 않고
// **프록시 데이터 소켓**(hostPath)을 받아 요청 레벨로 공유한다 (Exp_44/45).
//
// ★수량 규약 — 슬롯 광고: 프록시 PE 는 다중 테넌트 공유가 목적이므로 수량 1 이
// 아니라 **슬롯 npuModelSlots(10)개**를 광고한다 (수량 1 이면 파드 1개가 독점해
// 시분할 자체가 불가). 파드의 슬롯 수 = 시간 몫 표현 (7 슬롯 = duty 0.7 의향) —
// LSU 비례 배분과 동형. 실제 비율 집행은 controller feeder 등록(Exp_45)이 담당하며
// 슬롯 수는 스케줄링 힌트·계약 표현이다 (플러그인이 feeder 자동 등록까지 하지는
// 않음 — v1 범위, report §Phase 1).
//
// 이중계상 금지: 이 PE 는 npu.go 의 직접 할당 풀에서 제외된다 (NewNPUPlugin excludePE).
// fusion: PE 풀이 갈리므로 "수량 2 = fusion" 규약은 모델 리소스에 없음 — 직접 풀도
// 1 PE 뿐이라 fusion 은 본 구성에서 미지원 (PE 분할과 fusion 은 원래 상충, Exp_36/43).

import (
	"context"
	"encoding/json"
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
	npuModelSocketName = "kraken-npu-model.sock"
	npuModelSlots      = 10 // 슬롯 1개 = duty 0.1 의향 (시간 몫 표현 단위)
)

// NPUModelPlugin serves keti.io/npu-<model> backed by the proxy-owned PE.
type NPUModelPlugin struct {
	pluginapi.UnimplementedDevicePluginServer

	model    string // "resnet50"
	pe       string // "npu0pe0" (프록시 전용 PE — 광고 ID 접두)
	proxyDir string // hostPath: data.sock/admin.sock/socks/logs (파드에 마운트)

	resourceName string
	socketPath   string
	sockName     string // kubelet Register Endpoint (모델별 — Exp_59)
	deviceIDs    []string

	// 생명주기 자동화 (Exp_53): Allocate → 프록시 admin register + feeder
	// register/arm. 실패는 경고만 (Allocate 를 막지 않음 — 파드 기동 우선).
	controller   *ControllerClient // nil 이면 자동 등록 비활성
	redistribute bool              // 정책 B opt-in (기본 A=release 만, Exp_53 Phase 1)
	allocs       map[string]int    // canonical 슬롯 ID → 슬롯 수 (해제·복원용)
	allocsMu     sync.Mutex

	server *grpc.Server
	stopCh chan struct{}
	wg     sync.WaitGroup
}

// NewNPUModelPlugin prepares the model-resource plugin.
// 소켓명은 첫 모델(하위호환) = kraken-npu-model.sock, 그 외 = 모델별 접미.
func NewNPUModelPlugin(model, pe, proxyDir string) *NPUModelPlugin {
	return newNPUModelPluginSock(model, pe, proxyDir, npuModelSocketName)
}

// NewNPUModelPluginN prepares the i-th model plugin (Exp_59 다중 모델).
// i==0 은 기존 소켓명을 그대로 써 라이브 교체 시 재등록 노이즈가 없다.
func NewNPUModelPluginN(model, pe, proxyDir string, i int) *NPUModelPlugin {
	name := npuModelSocketName
	if i > 0 {
		name = fmt.Sprintf("kraken-npu-model-%s.sock", model)
	}
	return newNPUModelPluginSock(model, pe, proxyDir, name)
}

func newNPUModelPluginSock(model, pe, proxyDir, sockName string) *NPUModelPlugin {
	p := &NPUModelPlugin{
		model:        model,
		pe:           pe,
		proxyDir:     proxyDir,
		resourceName: "keti.io/npu-" + model,
		socketPath:   filepath.Join(pluginSocketDir, sockName),
		sockName:     sockName,
		allocs:       make(map[string]int),
		stopCh:       make(chan struct{}),
	}
	for i := 0; i < npuModelSlots; i++ {
		p.deviceIDs = append(p.deviceIDs,
			fmt.Sprintf("keti-%s-%s-s%02d", pe, model, i))
	}
	return p
}

// Start serves gRPC and registers the model resource with kubelet.
func (p *NPUModelPlugin) Start() error {
	_ = os.Remove(p.socketPath)
	ln, err := net.Listen("unix", p.socketPath)
	if err != nil {
		return fmt.Errorf("NPU 모델 소켓 생성 실패 (%s): %w", p.socketPath, err)
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
				log.Printf("[npu-model] gRPC 서버 오류: %v", err)
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
		Endpoint:     p.sockName,
		ResourceName: p.resourceName,
	})
	if err != nil {
		p.server.Stop()
		return fmt.Errorf("kubelet 등록 실패 (%s): %w", p.resourceName, err)
	}

	log.Printf("[npu-model] 준비 완료: resource=%s pe=%s 슬롯=%d개 (프록시 경유)",
		p.resourceName, p.pe, npuModelSlots)
	return nil
}

// Stop shuts down the server and removes the socket.
func (p *NPUModelPlugin) Stop() {
	close(p.stopCh)
	if p.server != nil {
		p.server.Stop()
	}
	p.wg.Wait()
	_ = os.Remove(p.socketPath)
}

// ── gRPC 메서드 ───────────────────────────────────────────────────────────────

func (p *NPUModelPlugin) GetDevicePluginOptions(
	_ context.Context, _ *pluginapi.Empty,
) (*pluginapi.DevicePluginOptions, error) {
	return &pluginapi.DevicePluginOptions{}, nil
}

func (p *NPUModelPlugin) ListAndWatch(
	_ *pluginapi.Empty,
	stream pluginapi.DevicePlugin_ListAndWatchServer,
) error {
	resp := &pluginapi.ListAndWatchResponse{}
	for _, id := range p.deviceIDs {
		resp.Devices = append(resp.Devices, &pluginapi.Device{
			ID: id, Health: pluginapi.Healthy,
		})
	}
	if err := stream.Send(resp); err != nil {
		return err
	}
	<-p.stopCh
	return nil
}

// ── 생명주기 자동화 (Exp_53) ─────────────────────────────────────────────────

// adminSend sends a one-way command to the proxy admin socket (UDS DGRAM).
func (p *NPUModelPlugin) adminSend(msg string) error {
	conn, err := net.Dial("unixgram", filepath.Join(p.proxyDir, "admin.sock"))
	if err != nil {
		return err
	}
	defer conn.Close()
	_, err = conn.Write([]byte(msg))
	return err
}

// tenantPaths returns the feeder-facing sock/log paths for a tenant
// (npu-proxy 계약: <proxyDir>/socks/<t>.sock, <proxyDir>/logs/<t>.log).
func (p *NPUModelPlugin) tenantPaths(tenant string) (string, string) {
	return filepath.Join(p.proxyDir, "socks", tenant+".sock"),
		filepath.Join(p.proxyDir, "logs", tenant+".log")
}

// autoRegister wires a tenant into proxy+feeder: admin register(소켓 선생성)
// → feeder register(ratio=slots/10, resource=npu) → arm. 오류는 경고 반환만.
func (p *NPUModelPlugin) autoRegister(tenant string, slots int) {
	if err := p.adminSend("register " + tenant); err != nil {
		log.Printf("[npu-model] 경고: 프록시 admin register 실패 (%s): %v", tenant, err)
	}
	if p.controller == nil {
		return
	}
	ratio := float64(slots) / float64(npuModelSlots)
	sock, logPath := p.tenantPaths(tenant)
	if err := p.controller.FeederRegister(tenant, sock, logPath, ratio, "npu"); err != nil {
		log.Printf("[npu-model] 경고: feeder register 실패 (%s): %v — 파드 기동은 계속", tenant, err)
		return
	}
	if err := p.controller.FeederArm(tenant); err != nil {
		log.Printf("[npu-model] 경고: feeder arm 실패 (%s): %v", tenant, err)
		return
	}
	p.allocsMu.Lock()
	p.allocs[tenant] = slots
	p.allocsMu.Unlock()
	log.Printf("[npu-model] 자동 등록: tenant=%s ratio=%.1f (slots=%d)", tenant, ratio, slots)
}

// CleanupTenant tears a tenant down: feeder release+deregister + 프록시
// unregister (+정책 B 면 남은 테넌트 재정규화). 부분 실패는 로그만 —
// 다음 정리 기회(checkpoint 재동기화)에서 복구 가능 (Exp_53 Phase 3).
func (p *NPUModelPlugin) CleanupTenant(tenant string) {
	if p.controller != nil {
		if err := p.controller.FeederRelease(tenant); err != nil {
			log.Printf("[npu-model] 경고: feeder release 실패 (%s): %v", tenant, err)
		}
		if err := p.controller.FeederDeregister(tenant); err != nil {
			log.Printf("[npu-model] 경고: feeder deregister 실패 (%s): %v", tenant, err)
		}
	}
	if err := p.adminSend("unregister " + tenant); err != nil {
		log.Printf("[npu-model] 경고: 프록시 unregister 실패 (%s): %v", tenant, err)
	}
	p.allocsMu.Lock()
	delete(p.allocs, tenant)
	remaining := make(map[string]int, len(p.allocs))
	for t, n := range p.allocs {
		remaining[t] = n
	}
	p.allocsMu.Unlock()
	log.Printf("[npu-model] 자동 해제: tenant=%s (정책=%s)", tenant,
		map[bool]string{true: "B 재정규화", false: "A 계약 유지"}[p.redistribute])
	// 정책 B (opt-in): 빈 몫을 남은 테넌트가 슬롯 비례로 나눔 (Σslots≤10 은
	// K8s 가 보장하므로 초과 위험 없음 — Exp_53 Phase 1)
	if p.redistribute && p.controller != nil && len(remaining) > 0 {
		tot := 0
		for _, n := range remaining {
			tot += n
		}
		ratios := make(map[string]float64, len(remaining))
		for t, n := range remaining {
			ratios[t] = float64(n) / float64(tot)
		}
		if err := p.controller.FeederRatios(ratios,
			"npu-model 이탈 재정규화 (정책 B)"); err != nil {
			log.Printf("[npu-model] 경고: 재정규화 실패: %v", err)
		}
	}
}

// IsSlotRegistered reports whether the canonical slot id is a live tenant.
func (p *NPUModelPlugin) IsSlotRegistered(id string) bool {
	p.allocsMu.Lock()
	defer p.allocsMu.Unlock()
	_, ok := p.allocs[id]
	return ok
}

// RestoreFromCheckpoint re-registers tenants for pods that already hold our
// slots (DS 재기동 시 controller/feeder/프록시 상태가 함께 초기화되므로 필수 —
// Exp_53 Phase 4-5 영속성). checkpoint 는 kubelet 이 소유한 진실이나 파드 삭제
// 후 엔트리가 지연 정리될 수 있으므로 ★liveUIDs(현재 노드의 비종료 파드)로
// 필터한다 — stale 엔트리 재등록이 고아 테넌트를 만드는 것을 차단 (Exp_53
// E2E 에서 실측된 결함의 수정).
func (p *NPUModelPlugin) RestoreFromCheckpoint(path string, liveUIDs map[string]bool) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var cp checkpointData
	if err := json.Unmarshal(raw, &cp); err != nil {
		return
	}
	n, stale := 0, 0
	for _, e := range cp.Data.PodDeviceEntries {
		if e.ResourceName != p.resourceName {
			continue
		}
		if liveUIDs != nil && !liveUIDs[e.PodUID] {
			stale++
			continue
		}
		ids := e.allDeviceIDs()
		if len(ids) == 0 {
			continue
		}
		sort.Strings(ids)
		p.autoRegister(ids[0], len(ids))
		n++
	}
	if n > 0 || stale > 0 {
		log.Printf("[npu-model] checkpoint 복원: 재등록 %d건, stale 제외 %d건", n, stale)
	}
}

// modelAllocateOne computes envs/mount for one container request.
// 디바이스 노드는 부여하지 않는다 — PE 는 프록시 소유, 파드는 소켓 클라이언트.
func (p *NPUModelPlugin) modelAllocateOne(ids []string) (map[string]string, []*pluginapi.Mount, error) {
	if len(ids) == 0 || len(ids) > npuModelSlots {
		return nil, nil, fmt.Errorf("슬롯 요청 1..%d 지원: %d", npuModelSlots, len(ids))
	}
	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)
	canonical := sorted[0] // 테넌트 ID = canonical 슬롯 (GPU LSU 경로와 동형, Exp_41 위생)
	envs := map[string]string{
		"KETI_NPU_PROXY_MODEL":     p.model,
		"KETI_NPU_PROXY_PE":        p.pe,
		"KETI_NPU_PROXY_DATA_SOCK": filepath.Join(p.proxyDir, "data.sock"),
		"KETI_NPU_PROXY_TENANT":    canonical,
		// 슬롯 수 = 시간 몫 의향 (duty = slots/10). feeder 등록 시 time_ratio 로 사용.
		"KETI_NPU_PROXY_SLOTS": fmt.Sprintf("%d", len(ids)),
		"KETI_NPU_PROXY_SHARE": fmt.Sprintf("%.1f",
			float64(len(ids))/float64(npuModelSlots)),
	}
	mounts := []*pluginapi.Mount{{
		HostPath:      p.proxyDir,
		ContainerPath: p.proxyDir,
		ReadOnly:      false, // data.sock 연결에 쓰기 필요
	}}
	return envs, mounts, nil
}

// Allocate injects proxy socket + tenant identity (디바이스 노드 없음).
func (p *NPUModelPlugin) Allocate(
	_ context.Context,
	reqs *pluginapi.AllocateRequest,
) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}
	for _, creq := range reqs.ContainerRequests {
		envs, mounts, err := p.modelAllocateOne(creq.DevicesIds)
		if err != nil {
			return nil, err
		}
		resp.ContainerResponses = append(resp.ContainerResponses,
			&pluginapi.ContainerAllocateResponse{Envs: envs, Mounts: mounts})
		log.Printf("[npu-model] Allocate: slots=%d tenant=%s share=%s (proxy=%s)",
			len(creq.DevicesIds), envs["KETI_NPU_PROXY_TENANT"],
			envs["KETI_NPU_PROXY_SHARE"], p.pe)
		// Exp_53: 슬롯 → feeder 자동 등록 (실패해도 Allocate 는 성공)
		p.autoRegister(envs["KETI_NPU_PROXY_TENANT"], len(creq.DevicesIds))
	}
	return resp, nil
}

func (p *NPUModelPlugin) GetPreferredAllocation(
	_ context.Context,
	req *pluginapi.PreferredAllocationRequest,
) (*pluginapi.PreferredAllocationResponse, error) {
	return &pluginapi.PreferredAllocationResponse{}, nil
}

func (p *NPUModelPlugin) PreStartContainer(
	_ context.Context,
	_ *pluginapi.PreStartContainerRequest,
) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}
