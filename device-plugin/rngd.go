package main

// rngd.go — RNGD PE 단위 Device Plugin (Exp_70, 설계: Exp_69 npu_gate_v2 §D-1).
//
// 광고: keti.re.kr/npu-pe = PE 단위 device (RNGD 1장 = 8 devices).
//   리소스명은 keti.re.kr 계보를 따른다 (구 keti.io/npu* 는 Warboy 전제 — 미승계).
// 정렬 제약 (Exp_67 B-4 실측): fusion 은 정렬 블록 {1,2,4,8} 만 성립한다.
//   K8s Extended Resource 카운트는 이를 표현하지 못하므로 (Exp_70 Phase 2 3안 비교
//   → (iii) 채택) 할당 시점에 강제한다:
//     - GetPreferredAllocation 이 정렬 블록을 선호 반환 (kubelet 이 통상 존중)
//     - Allocate 가 수량·정렬을 검증하고 위반 시 명시적 오류 (실행 불가 파드 차단)
// 부여 세트 (외부 검증 rngd_verify_20260803 phase2 §5.2 실측 — 분할 최소 집합):
//   블록 내 PE 노드 + 블록 내 fusion 노드(존재분) + BASE(mgmt,bar0/2/4,dmar,p2pmem)
//   + DMA ch 16개 전부(ch0..7, ch0r..7r — 부분 마운트는 실행 실패 실측).
//   격리는 커널이 강제한다: 블록 밖 PE 노드가 없으면 컨테이너가 쓸 수 없다.

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

const (
	rngdSocketName   = "kraken-rngd.sock"
	rngdResourceName = "keti.re.kr/npu-pe"
)

// rngdAlignedSizes 는 허용 요청 수량 (fusion 입도 실측 {1,2,4,8} — 3·5·6·7 은
// 런타임이 "not supported count of PEs" 로 거부).
var rngdAlignedSizes = map[int]bool{1: true, 2: true, 4: true, 8: true}

// RNGDDevice is one advertised PE.
type RNGDDevice struct {
	NpuIdx  int
	PEIdx   int
	DevPath string // /dev/rngd/npu1pe0
}

func rngdDeviceID(npuIdx, peIdx int) string {
	return fmt.Sprintf("keti-rngd%d-pe%d", npuIdx, peIdx)
}

func parseRNGDDeviceID(id string) (npuIdx, peIdx int, err error) {
	_, err = fmt.Sscanf(id, "keti-rngd%d-pe%d", &npuIdx, &peIdx)
	return npuIdx, peIdx, err
}

// DiscoverRNGDPEs scans devRoot for RNGD PE nodes (npu{i}pe{j} — 구분자 없음,
// Warboy(/dev/npu0pe0)와 달리 /dev/rngd/ 아래에 있다).
func DiscoverRNGDPEs(devRoot string) ([]RNGDDevice, error) {
	var out []RNGDDevice
	for npu := 0; npu < maxNPUScan; npu++ {
		for pe := 0; pe < maxPEScan; pe++ {
			p := filepath.Join(devRoot, fmt.Sprintf("npu%dpe%d", npu, pe))
			if _, err := os.Stat(p); err == nil {
				out = append(out, RNGDDevice{NpuIdx: npu, PEIdx: pe, DevPath: p})
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("RNGD PE 디바이스 없음 (%s/npu*pe*)", devRoot)
	}
	return out, nil
}

// rngdCompanions returns BASE + 전체 DMA ch 노드 (부분 불가 — 실측).
// BASE 중 mgmt 는 필수, 나머지는 존재분만.
func rngdCompanions(devRoot string, npuIdx int) ([]string, error) {
	mgmt := filepath.Join(devRoot, fmt.Sprintf("npu%dmgmt", npuIdx))
	if _, err := os.Stat(mgmt); err != nil {
		return nil, fmt.Errorf("mgmt 노드 없음 (%s) — SDK 구동 불가 세트", mgmt)
	}
	out := []string{mgmt}
	for _, suffix := range []string{"bar0", "bar2", "bar4", "dmar", "p2pmem"} {
		p := filepath.Join(devRoot, fmt.Sprintf("npu%d%s", npuIdx, suffix))
		if _, err := os.Stat(p); err == nil {
			out = append(out, p)
		}
	}
	// DMA ch 16개 — 하나라도 없으면 실행이 실패하므로 필수 검증.
	for ch := 0; ch < 8; ch++ {
		for _, sfx := range []string{"", "r"} {
			p := filepath.Join(devRoot, fmt.Sprintf("npu%dch%d%s", npuIdx, ch, sfx))
			if _, err := os.Stat(p); err != nil {
				return nil, fmt.Errorf("DMA ch 노드 없음 (%s) — 전체 16개 필수 (실측)", p)
			}
			out = append(out, p)
		}
	}
	return out, nil
}

// rngdBlockFusionNodes returns fusion node paths fully contained in [lo,hi]
// (존재분만 — 8PE 전체 fusion 노드(pe0-7)는 실기기에 없다, Exp_67).
func rngdBlockFusionNodes(devRoot string, npuIdx, lo, hi int) []string {
	var out []string
	for a := lo; a < hi; a++ {
		for b := a + 1; b <= hi; b++ {
			p := filepath.Join(devRoot, fmt.Sprintf("npu%dpe%d-%d", npuIdx, a, b))
			if _, err := os.Stat(p); err == nil {
				out = append(out, p)
			}
		}
	}
	return out
}

// rngdValidateBlock checks ids form an aligned block; returns (npuIdx, lo, hi).
func rngdValidateBlock(ids []string) (int, int, int, error) {
	k := len(ids)
	if !rngdAlignedSizes[k] {
		return 0, 0, 0, fmt.Errorf(
			"PE 요청 수량 %d 불가 — fusion 정렬 제약상 1/2/4/8 만 허용 (Exp_67 실측)", k)
	}
	npuIdx := -1
	pes := make([]int, 0, k)
	for _, id := range ids {
		n, pe, err := parseRNGDDeviceID(id)
		if err != nil {
			return 0, 0, 0, fmt.Errorf("device ID 파싱 실패 (%s): %w", id, err)
		}
		if npuIdx == -1 {
			npuIdx = n
		} else if n != npuIdx {
			return 0, 0, 0, fmt.Errorf("요청이 여러 NPU 에 걸침: %v", ids)
		}
		pes = append(pes, pe)
	}
	sort.Ints(pes)
	lo, hi := pes[0], pes[len(pes)-1]
	if hi-lo+1 != k || lo%k != 0 {
		return 0, 0, 0, fmt.Errorf(
			"비정렬 PE 조합 %v — 크기 %d 는 시작 인덱스 %d 배수의 연속 블록만 가능", pes, k, k)
	}
	for i := 1; i < len(pes); i++ {
		if pes[i] == pes[i-1] {
			return 0, 0, 0, fmt.Errorf("중복 PE: %v", pes)
		}
	}
	return npuIdx, lo, hi, nil
}

// RNGDPlugin implements the device-plugin API for keti.re.kr/npu-pe.
type RNGDPlugin struct {
	pluginapi.UnimplementedDevicePluginServer

	devRoot    string
	socketPath string
	devices    map[string]RNGDDevice

	server *grpc.Server
	stopCh chan struct{}
	wg     sync.WaitGroup

	// [Exp_122 4부] 가용 PE 동적 노출
	resv     *reservations
	occEvery time.Duration
	lastOcc  PEOccupancy
	occMu    sync.Mutex
}

// NewRNGDPlugin discovers PEs under devRoot (기본 /dev/rngd).
func NewRNGDPlugin(devRoot string) (*RNGDPlugin, error) {
	pes, err := DiscoverRNGDPEs(devRoot)
	if err != nil {
		return nil, err
	}
	p := &RNGDPlugin{
		devRoot:    devRoot,
		socketPath: filepath.Join(pluginSocketDir, rngdSocketName),
		devices:    make(map[string]RNGDDevice),
		stopCh:     make(chan struct{}),
		// 예약 TTL — 파드 배치부터 컨테이너가 PE 를 열기까지의 공백을 덮는다.
		//   실측 로드 시간 6.9~8.4초(Exp_122 2-A/2-B/3-A)의 약 4배를 둔다.
		//   짧으면 공백이 뚫리고, 길면 기동 실패한 파드의 예약이 오래 남는다.
		resv:     newReservations(30 * time.Second),
		occEvery: rngdOccInterval(),
	}
	for _, d := range pes {
		// companion 완비 검증을 기동 시점에 수행 — 결손 상태로 광고하지 않는다.
		if _, err := rngdCompanions(devRoot, d.NpuIdx); err != nil {
			return nil, err
		}
		p.devices[rngdDeviceID(d.NpuIdx, d.PEIdx)] = d
		log.Printf("[rngd] 발견: %s (%s)", rngdDeviceID(d.NpuIdx, d.PEIdx), d.DevPath)
	}
	return p, nil
}

// Start serves the gRPC endpoint and registers keti.re.kr/npu-pe with kubelet.
func (p *RNGDPlugin) Start() error {
	_ = os.Remove(p.socketPath)
	ln, err := net.Listen("unix", p.socketPath)
	if err != nil {
		return fmt.Errorf("RNGD 소켓 생성 실패 (%s): %w", p.socketPath, err)
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
				log.Printf("[rngd] gRPC 서버 오류: %v", err)
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
		Endpoint:     rngdSocketName,
		ResourceName: rngdResourceName,
	})
	if err != nil {
		p.server.Stop()
		return fmt.Errorf("kubelet 등록 실패 (rngd): %w", err)
	}

	log.Printf("[rngd] 준비 완료: resource=%s PE=%d개", rngdResourceName, len(p.devices))
	return nil
}

// Stop shuts down the server and removes the socket.
func (p *RNGDPlugin) Stop() {
	close(p.stopCh)
	if p.server != nil {
		p.server.Stop()
	}
	p.wg.Wait()
	_ = os.Remove(p.socketPath)
}

// ── gRPC 메서드 ───────────────────────────────────────────────────────────────

func (p *RNGDPlugin) GetDevicePluginOptions(
	_ context.Context, _ *pluginapi.Empty,
) (*pluginapi.DevicePluginOptions, error) {
	return &pluginapi.DevicePluginOptions{
		PreStartRequired:                false,
		GetPreferredAllocationAvailable: true, // 정렬 블록 선호 — (iii)의 1차 방어선
	}, nil
}

func (p *RNGDPlugin) ListAndWatch(
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
	// [Exp_122 4부] 점유를 주기적으로 스캔해 가용 PE 만 Healthy 로 광고한다.
	//   갱신 주기는 환경 변수로 노출한다(기본 5초 — §4 반영 지연 측정 근거).
	ticker := time.NewTicker(p.occEvery)
	defer ticker.Stop()
	prev := ""
	nscan := 0
	for {
		select {
		case <-p.stopCh:
			return nil
		case <-stream.Context().Done():
			return nil
		case <-ticker.C:
			// ★스캔은 반드시 시한 안에 끝나야 한다. 그래도 못 돌아오는 경우를 대비해
			//   결과를 채널로 받고, 늦으면 이번 주기를 건너뛴다(루프는 계속 돈다).
			ch := make(chan PEOccupancy, 1)
			go func() { ch <- scanSMIPS(4 * time.Second) }()
			var occ PEOccupancy
			select {
			case occ = <-ch:
			case <-time.After(6 * time.Second):
				rngdWarn("smi_stall", "furiosa-smi ps 가 6초 안에 돌아오지 않는다 — "+
					"이번 주기를 건너뛴다. 노출 값이 실제 점유보다 클 수 있다")
				continue
			}
			p.occMu.Lock()
			p.lastOcc = occ
			p.occMu.Unlock()
			nscan++
			if nscan <= 3 || nscan%60 == 0 {
				log.Printf("[rngd] 점유 스캔 #%d: %dms 점유PE=%d err=%v",
					nscan, occ.TookMS, len(occ.Busy), occ.Err)
			}
			if occ.Err != nil {
				// [Exp_107 T-5] 관측 실패를 조용히 '유휴' 로 넘기지 않는다.
				rngdWarn("smi_ps", fmt.Sprintf(
					"%v — 이 주기는 외부 점유를 반영하지 못한다. 우리 예약 기록만 적용된다",
					occ.Err))
			}
			health := p.healthOf(occ, p.resv.active())
			r := &pluginapi.ListAndWatchResponse{}
			var sig []string
			for id, d := range p.devices {
				st := pluginapi.Healthy
				if _, err := os.Stat(d.DevPath); err != nil {
					st = pluginapi.Unhealthy // 디바이스 소실
				} else if health[id] != "" {
					st = pluginapi.Unhealthy // 점유 중
				}
				r.Devices = append(r.Devices, &pluginapi.Device{ID: id, Health: st})
				sig = append(sig, id+"="+st)
			}
			sort.Slice(r.Devices, func(i, j int) bool {
				return r.Devices[i].ID < r.Devices[j].ID
			})
			sort.Strings(sig)
			cur := strings.Join(sig, ",")
			if cur == prev {
				continue // 변화 없으면 보내지 않는다
			}
			prev = cur
			n := 0
			for _, d := range r.Devices {
				if d.Health == pluginapi.Healthy {
					n++
				}
			}
			log.Printf("[rngd] 가용 PE 갱신: %d/%d (점유: %v)", n, len(r.Devices),
				rngdBusySummary(health))
			if err := stream.Send(r); err != nil {
				return err
			}
		}
	}
}

// rngdOccInterval 은 점유 스캔 주기다. 기본 5초.
func rngdOccInterval() time.Duration {
	if v := os.Getenv("KRAKEN_RNGD_OCC_INTERVAL_MS"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil && ms >= 500 {
			return time.Duration(ms) * time.Millisecond
		}
		rngdWarn("occ_interval", fmt.Sprintf(
			"KRAKEN_RNGD_OCC_INTERVAL_MS=%q 를 해석할 수 없다(500 이상 정수 ms) → 기본 5000ms 사용", v))
	}
	return 5 * time.Second
}

func rngdBusySummary(health map[string]string) []string {
	var out []string
	for id, why := range health {
		if why != "" {
			out = append(out, id+"←"+why)
		}
	}
	sort.Strings(out)
	return out
}

// rngdPreferredBlock picks the lowest fully-available aligned block of size k.
// avail 은 사용 가능 device ID 목록. 반환: 블록의 ID 목록 (없으면 오류).
func rngdPreferredBlock(avail []string, k int) ([]string, error) {
	if !rngdAlignedSizes[k] {
		return nil, fmt.Errorf(
			"PE 요청 수량 %d 불가 — 1/2/4/8 만 허용 (fusion 정렬 제약, Exp_67 실측)", k)
	}
	byNpu := map[int]map[int]string{}
	for _, id := range avail {
		if n, pe, err := parseRNGDDeviceID(id); err == nil {
			if byNpu[n] == nil {
				byNpu[n] = map[int]string{}
			}
			byNpu[n][pe] = id
		}
	}
	npus := make([]int, 0, len(byNpu))
	for n := range byNpu {
		npus = append(npus, n)
	}
	sort.Ints(npus)
	for _, n := range npus {
		for lo := 0; lo+k-1 < maxPEScan; lo += k {
			ids := make([]string, 0, k)
			for pe := lo; pe < lo+k; pe++ {
				if id, ok := byNpu[n][pe]; ok {
					ids = append(ids, id)
				}
			}
			if len(ids) == k {
				return ids, nil
			}
		}
	}
	return nil, fmt.Errorf("크기 %d 정렬 블록 가용 없음 (avail=%v) — 단편화", k, avail)
}

func (p *RNGDPlugin) GetPreferredAllocation(
	_ context.Context,
	req *pluginapi.PreferredAllocationRequest,
) (*pluginapi.PreferredAllocationResponse, error) {
	resp := &pluginapi.PreferredAllocationResponse{}
	for _, creq := range req.ContainerRequests {
		ids, err := rngdPreferredBlock(creq.AvailableDeviceIDs, int(creq.AllocationSize))
		if err != nil {
			// 오류 반환 시 kubelet 이 자체 선택으로 폴백할 수 있으나, 그 경우도
			// Allocate 검증이 최종 차단한다 — 명시 오류가 디버깅에 낫다.
			return nil, err
		}
		resp.ContainerResponses = append(resp.ContainerResponses,
			&pluginapi.ContainerPreferredAllocationResponse{DeviceIDs: ids})
	}
	return resp, nil
}

// rngdAllocateOne computes the device set + envs for one container request.
func (p *RNGDPlugin) rngdAllocateOne(ids []string) ([]string, map[string]string, error) {
	npuIdx, lo, hi, err := rngdValidateBlock(ids)
	if err != nil {
		return nil, nil, err
	}
	var paths []string
	for pe := lo; pe <= hi; pe++ {
		paths = append(paths, filepath.Join(p.devRoot,
			fmt.Sprintf("npu%dpe%d", npuIdx, pe)))
	}
	paths = append(paths, rngdBlockFusionNodes(p.devRoot, npuIdx, lo, hi)...)
	comp, err := rngdCompanions(p.devRoot, npuIdx)
	if err != nil {
		return nil, nil, err
	}
	paths = append(paths, comp...)

	rangeStr := fmt.Sprintf("%d", lo)
	if hi > lo {
		rangeStr = fmt.Sprintf("%d-%d", lo, hi)
	}
	envs := map[string]string{
		// furiosa-llm --devices 형식 (외부 검증 §8 권고 운용 — npu:1:0-3)
		"KETI_NPU_DEVICES":  fmt.Sprintf("npu:%d:%s", npuIdx, rangeStr),
		"KETI_NPU_PE_RANGE": rangeStr,
		"KETI_NPU_PE_COUNT": fmt.Sprintf("%d", hi-lo+1),
	}
	return paths, envs, nil
}

// Allocate grants 블록 PE + 블록 내 fusion + BASE + DMA ch 전체.
func (p *RNGDPlugin) Allocate(
	_ context.Context,
	reqs *pluginapi.AllocateRequest,
) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}
	for _, creq := range reqs.ContainerRequests {
		paths, envs, err := p.rngdAllocateOne(creq.DevicesIds)
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
		// [Exp_122 4부] 예약 기록 — 컨테이너가 PE 를 열기 전 공백을 메꾼다.
		p.resv.add(creq.DevicesIds)
		log.Printf("[rngd] Allocate: ids=%v → devices=%d개 env=%s (예약 %s)",
			creq.DevicesIds, len(paths), envs["KETI_NPU_DEVICES"], p.resv.ttl)
		resp.ContainerResponses = append(resp.ContainerResponses, car)
	}
	return resp, nil
}

func (p *RNGDPlugin) PreStartContainer(
	_ context.Context,
	_ *pluginapi.PreStartContainerRequest,
) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}
