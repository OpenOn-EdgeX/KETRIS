package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"google.golang.org/grpc"
	api "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

type Config struct {
	Node         string `json:"node"`
	GPUUUID      string `json:"gpu_uuid"`
	MIGUUID      string `json:"mig_uuid"`
	Generation   string `json:"generation"`
	ResourceName string `json:"resource_name"`
	StateDir     string `json:"state_dir"`
	PipeDir      string `json:"pipe_dir"`
	LibPath      string `json:"lib_path"`
	CudaLibDir   string `json:"cuda_lib_dir"`
	Units        int    `json:"units"`
	Active       bool   `json:"active"`
}

type Plugin struct {
	api.UnimplementedDevicePluginServer
	Config     Config
	ConfigPath string
	mu         sync.Mutex
	server     *grpc.Server
	socket     string
}

func NewPlugin(c Config, path string) (*Plugin, error) {
	if !regexp.MustCompile(`^MIG-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).MatchString(c.MIGUUID) {
		return nil, fmt.Errorf("invalid MIG UUID")
	}
	sum := sha256.Sum256([]byte(c.MIGUUID))
	expected := "keti.re.kr/mig-" + hex.EncodeToString(sum[:])[:16] + "-share"
	if c.ResourceName != expected || c.Generation == "" || c.Node == "" || c.GPUUUID == "" {
		return nil, fmt.Errorf("invalid resource/generation/node identity")
	}
	if c.Units == 0 {
		c.Units = 200
	}
	if c.Units < 100 || c.Units > 200 {
		return nil, fmt.Errorf("logical share units must be in 100..200")
	}
	if !filepath.IsAbs(c.StateDir) || !filepath.IsAbs(c.PipeDir) || !filepath.IsAbs(c.LibPath) {
		return nil, fmt.Errorf("node state/MPS/runtime paths must be absolute")
	}
	if info, err := os.Stat(c.LibPath); err != nil || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("runtime library is unavailable")
	}
	return &Plugin{Config: c, ConfigPath: path}, nil
}

func (p *Plugin) DeviceID(unit int) string {
	return fmt.Sprintf("%s-u%03d", p.Config.ResourceName[len("keti.re.kr/mig-"):len("keti.re.kr/mig-")+16], unit)
}
func (p *Plugin) current() bool {
	if p.ConfigPath == "" {
		return p.Config.Active
	}
	data, err := os.ReadFile(p.ConfigPath)
	if err != nil {
		return false
	}
	var c Config
	if json.Unmarshal(data, &c) != nil {
		return false
	}
	return c.Active && c.MIGUUID == p.Config.MIGUUID && c.GPUUUID == p.Config.GPUUUID && c.Generation == p.Config.Generation && c.ResourceName == p.Config.ResourceName
}
func (p *Plugin) GetDevicePluginOptions(context.Context, *api.Empty) (*api.DevicePluginOptions, error) {
	return &api.DevicePluginOptions{}, nil
}
func (p *Plugin) ListAndWatch(_ *api.Empty, s api.DevicePlugin_ListAndWatchServer) error {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		health := api.Unhealthy
		if p.current() {
			health = api.Healthy
		}
		var devices []*api.Device
		for i := 0; i < p.Config.Units; i++ {
			devices = append(devices, &api.Device{ID: p.DeviceID(i), Health: health})
		}
		if err := s.Send(&api.ListAndWatchResponse{Devices: devices}); err != nil {
			return err
		}
		select {
		case <-s.Context().Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (p *Plugin) Allocate(ctx context.Context, r *api.AllocateRequest) (*api.AllocateResponse, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.current() {
		return nil, fmt.Errorf("MIG inventory is stale or inactive")
	}
	valid := map[string]bool{}
	for i := 0; i < p.Config.Units; i++ {
		valid[p.DeviceID(i)] = true
	}
	seen := map[string]bool{}
	for _, c := range r.ContainerRequests {
		if len(c.DevicesIds) < 1 || len(c.DevicesIds) > 100 {
			return nil, fmt.Errorf("space request must be 1..100 logical units")
		}
		for _, id := range c.DevicesIds {
			if !valid[id] || seen[id] {
				return nil, fmt.Errorf("invalid/mixed/duplicate MIG share ID")
			}
			seen[id] = true
		}
	}
	out := &api.AllocateResponse{}
	for _, c := range r.ContainerRequests {
		b, err := p.lease(c.DevicesIds)
		deadline := time.Now().Add(10 * time.Second)
		for errors.Is(err, errRetiringOwner) && time.Now().Before(deadline) && p.current() {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(50 * time.Millisecond):
			}
			b, err = p.lease(c.DevicesIds)
		}
		if err != nil {
			return nil, err
		}
		if err = os.MkdirAll(p.Config.PipeDir, 0700); err != nil {
			return nil, err
		}
		out.ContainerResponses = append(out.ContainerResponses, &api.ContainerAllocateResponse{
			Envs: map[string]string{"CUDA_VISIBLE_DEVICES": p.Config.MIGUUID, "NVIDIA_VISIBLE_DEVICES": "void", "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": fmt.Sprint(b.SpacePercent), "CUDA_MPS_PIPE_DIRECTORY": p.Config.PipeDir,
				"BLESS_SPACE_MODE": "mps_env", "BLESS_TIME_START_GATED": "1", "BLESS_TIME_WC": "0", "BLESS_GATE_METRICS": "1", "BLESS_TENANT": b.AllocationID, "KETRIS_ALLOCATION_ID": b.AllocationID,
				"LD_PRELOAD": "/ketris/libbless.so", "LD_LIBRARY_PATH": "/ketris/cuda-lib", "BLESS_STATS_LOG": "/tmp/feed.log", "KETRIS_DEVICE_INFO_LOG": "/tmp/device.json"},
			CdiDevices: []*api.CDIDevice{{Name: "nvidia.com/gpu=" + p.Config.MIGUUID}},
			Mounts:     []*api.Mount{{HostPath: p.Config.LibPath, ContainerPath: "/ketris/libbless.so", ReadOnly: true}, {HostPath: p.Config.CudaLibDir, ContainerPath: "/ketris/cuda-lib", ReadOnly: true}, {HostPath: p.Config.PipeDir, ContainerPath: p.Config.PipeDir}, {HostPath: b.ControlDir, ContainerPath: "/tmp"}},
		})
	}
	return out, nil
}

func (p *Plugin) Start(socketDir string) error {
	short := p.Config.ResourceName[len("keti.re.kr/mig-") : len("keti.re.kr/mig-")+16]
	p.socket = filepath.Join(socketDir, "ketris-mig-"+short+".sock")
	os.Remove(p.socket)
	listener, err := net.Listen("unix", p.socket)
	if err != nil {
		return err
	}
	p.server = grpc.NewServer()
	api.RegisterDevicePluginServer(p.server, p)
	go p.server.Serve(listener)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(ctx, "unix://"+filepath.Join(socketDir, "kubelet.sock"), grpc.WithInsecure(), grpc.WithBlock())
	if err != nil {
		p.Stop()
		return err
	}
	defer conn.Close()
	_, err = api.NewRegistrationClient(conn).Register(ctx, &api.RegisterRequest{Version: api.Version, Endpoint: filepath.Base(p.socket), ResourceName: p.Config.ResourceName})
	if err != nil {
		p.Stop()
	}
	return err
}
func (p *Plugin) Stop() {
	if p.server != nil {
		p.server.Stop()
	}
	if p.socket != "" {
		os.Remove(p.socket)
	}
}
