package main

import (
	"bytes"
	"errors"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ControllerClient wraps HTTP calls to kraken_controller.py.
type ControllerClient struct {
	baseURL string
	client  *http.Client
}

// ControllerStatus mirrors the /status JSON response.
type ControllerStatus struct {
	GroupID        string  `json:"group_id"`
	Mode           string  `json:"mode"`
	PhysicalSM     int32   `json:"physical_sm"`
	PhysicalMemMB  int64   `json:"physical_mem_mb"`
	TenantCount    int     `json:"tenant_count"`
	PolicyVersion  uint64  `json:"policy_version"`
}

type registerReq struct {
	TenantID     string  `json:"tenant_id"`
	VirtualSM    int32   `json:"virtual_sm"`
	VirtualMemMB int32   `json:"virtual_mem_mb"`
	Weight       float64 `json:"weight"`
}

type registerResp struct {
	TenantIdx int               `json:"tenant_idx"`
	Env       map[string]string `json:"env"`
}

type deregisterReq struct {
	TenantIdx int `json:"tenant_idx"`
}

type deregisterByIDReq struct {
	TenantID string `json:"tenant_id"`
}

// NewControllerClient creates a client targeting the given base URL.
func NewControllerClient(baseURL string) *ControllerClient {
	return &ControllerClient{
		baseURL: baseURL,
		client: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// GetStatus fetches global GPU and scheduler status from the controller.
func (c *ControllerClient) GetStatus() (*ControllerStatus, error) {
	resp, err := c.client.Get(c.baseURL + "/status")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)
	}

	var s ControllerStatus
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return nil, fmt.Errorf("응답 파싱 실패: %w", err)
	}
	return &s, nil
}

// Register calls POST /register and returns the assigned tenant_idx.
// virtualMemMB is int32 to match the JSON schema (int field).
func (c *ControllerClient) Register(
	tenantID string,
	virtualSM int32,
	virtualMemMB int32,
	weight float64,
) (int, error) {
	body, err := json.Marshal(registerReq{
		TenantID:     tenantID,
		VirtualSM:    virtualSM,
		VirtualMemMB: virtualMemMB,
		Weight:       weight,
	})
	if err != nil {
		return 0, err
	}

	resp, err := c.client.Post(
		c.baseURL+"/register",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return 0, fmt.Errorf("POST /register 실패: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return 0, fmt.Errorf("POST /register HTTP %d: %s", resp.StatusCode, b)
	}

	var reg registerResp
	if err := json.NewDecoder(resp.Body).Decode(&reg); err != nil {
		return 0, fmt.Errorf("register 응답 파싱 실패: %w", err)
	}
	return reg.TenantIdx, nil
}

// Deregister calls POST /deregister with the numeric tenant slot index.
func (c *ControllerClient) Deregister(tenantIdx int) error {
	body, _ := json.Marshal(deregisterReq{TenantIdx: tenantIdx})

	resp, err := c.client.Post(
		c.baseURL+"/deregister",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("POST /deregister 실패: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("POST /deregister HTTP %d: %s", resp.StatusCode, b)
	}
	return nil
}

// ErrTenantNotFound marks a /deregister_by_id 404 — the ID is not a registered
// tenant. Under LSU mode this is a NORMAL outcome for all but the canonical
// (first) device ID of an allocation (Exp_41 O-2/watcher 위생).
var ErrTenantNotFound = errors.New("tenant_id not registered (HTTP 404)")

// DeregisterByTenantID calls POST /deregister_by_id with the tenant_id string.
// This is used by the pod watcher when it knows the device ID but not the slot index.
func (c *ControllerClient) DeregisterByTenantID(tenantID string) error {
	body, _ := json.Marshal(deregisterByIDReq{TenantID: tenantID})

	resp, err := c.client.Post(
		c.baseURL+"/deregister_by_id",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("POST /deregister_by_id 실패: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return fmt.Errorf("deregister_by_id %s: %w", tenantID, ErrTenantNotFound)
	}
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("POST /deregister_by_id HTTP %d: %s", resp.StatusCode, b)
	}
	return nil
}

// ── feeder 라우트 클라이언트 (Exp_53 — NPU 모델 경로 생명주기 자동화) ────────

// feederPost posts a JSON body to a /feeder/* route. 실패는 호출측이 경고 처리
// (Allocate 를 막지 않는다 — Exp_53 제약).
func (c *ControllerClient) feederPost(path string, body map[string]interface{}) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	resp, err := c.client.Post(c.baseURL+path, "application/json",
		bytes.NewReader(raw))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%s → HTTP %d: %s", path, resp.StatusCode, string(b))
	}
	return nil
}

// FeederRegister registers a tenant with the time-credit feeder (resource 태그 포함).
func (c *ControllerClient) FeederRegister(tenant, sock, logPath string,
	ratio float64, resource string) error {
	return c.feederPost("/feeder/register", map[string]interface{}{
		"tenant": tenant, "sock": sock, "log": logPath,
		"time_ratio": ratio, "resource": resource})
}

// FeederArm arms the gate for a tenant.
func (c *ControllerClient) FeederArm(tenant string) error {
	return c.feederPost("/feeder/arm", map[string]interface{}{"tenant": tenant})
}

// FeederRelease releases (unlimited) a tenant's gate.
func (c *ControllerClient) FeederRelease(tenant string) error {
	return c.feederPost("/feeder/release", map[string]interface{}{"tenant": tenant})
}

// FeederDeregister removes a tenant from the feeder (Exp_53 신설 라우트).
func (c *ControllerClient) FeederDeregister(tenant string) error {
	return c.feederPost("/feeder/deregister", map[string]interface{}{"tenant": tenant})
}

// BoosterRegister registers a tenant with the Turnaround Booster (Exp_134).
//   alivePath = 생존 신호 glob. pid 는 Allocate 시점에 알 수 없어 0 을 보낸다.
func (c *ControllerClient) BoosterRegister(tenant, gpu, alivePath string) error {
	return c.feederPost("/booster/register", map[string]interface{}{
		"tenant": tenant, "pid": 0, "gpu": gpu, "alive_path": alivePath})
}

// BoosterDeregister removes a tenant from the Booster (Exp_134 — devID 재사용 오염 방지).
func (c *ControllerClient) BoosterDeregister(tenant string) error {
	return c.feederPost("/booster/deregister", map[string]interface{}{"tenant": tenant})
}

// FeederRatios updates target ratios (재분배 정책 B — opt-in).
// ModeEvent reports a ③ mode transition so the controller can emit a K8s Event
// (Exp_138 2-B 5). 발행 실패는 에러로 올라가고 호출자가 경고를 남긴다 — 제어 자체는
// 이미 적용된 뒤다.
func (c *ControllerClient) ModeEvent(tenant, mode, reason, class string) error {
	return c.feederPost("/feeder/mode_event", map[string]interface{}{
		"tenant": tenant, "mode": mode, "reason": reason, "class": class})
}

// SliceEvent emits SliceCreated/SliceDestroyed via the same mode_event route
// (Exp_140 3부 — 새 발행 경로를 만들지 않는다). reasonKind 는 컨트롤러 쪽
// 화이트리스트로 검증된다. 발행 실패는 에러로 올라가고 호출자는 로그만 남긴다 —
// Allocate·해제를 막지 않는다.
func (c *ControllerClient) SliceEvent(reasonKind, tenant, podNS, podName, podUID, detail string) error {
	return c.feederPost("/feeder/mode_event", map[string]interface{}{
		"reason_kind": reasonKind, "tenant": tenant,
		"pod": podName, "pod_ns": podNS, "pod_uid": podUID, "detail": detail})
}

func (c *ControllerClient) FeederRatios(ratios map[string]float64, reason string) error {
	// [Exp_141] lease_s=0 = 무기한 opt-out. 이 클라이언트의 유일한 호출자는
	// npu_model 정책 B(이탈 재정규화)로, 재설정 자체가 **새 계약**이라 만료
	// 복귀 대상이 아니다. feeder 기본 임대(3s)가 걸리면 재정규화가 3초 뒤
	// 옛 계약으로 되돌아가 정책 B 가 무력화된다.
	return c.feederPost("/feeder/ratios", map[string]interface{}{
		"ratios": ratios, "reason": reason, "lease_s": 0})
}
