package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// catalogDevice is one entry under "devices" in lsu/catalog.json.
type catalogDevice struct {
	LSU            int     `json:"lsu"`
	ThroughputImgS float64 `json:"throughput_img_s"`
	Measured       bool    `json:"measured"`
	Note           string  `json:"note"`
}

// LSUCatalog mirrors lsu/catalog.json — the single source of truth that maps a
// physical device model name to its LSU (Logical Slice Unit) value.  It is
// produced/updated by lsu/benchmark.py and consumed here to decide how many
// logical units each GPU advertises to Kubernetes.
type LSUCatalog struct {
	ReferenceWorkload string                   `json:"reference_workload"`
	UnitImgPerS       float64                  `json:"unit_img_per_s"`
	UnitSource        string                   `json:"unit_source"`
	Status            string                   `json:"status"`
	Devices           map[string]catalogDevice `json:"devices"`
	DefaultLSU        int                      `json:"default_lsu"`
	// OvercommitFactor (Exp_73) — 광고 LSU = 물리 LSU × factor. factor>1 이면
	// K8s 에 물리보다 많은 논리 LSU 를 광고한다(오버커밋). 컴퓨트 pct 는 물리 LSU
	// 기준(Σ가 100% 초과 가능 = 오버커밋의 실체), 메모리 quota 는 광고 LSU 기준
	// (Σ가 물리를 절대 초과 안 함 — Exp_72 §4). 값은 catalog 설정이며 코드 상수 금지
	// (Exp_73 §0 — 1.5·267 을 박지 말 것). 미지정=1.0(Exp_71/72 동일 동작).
	OvercommitFactor float64 `json:"overcommit_factor"`
}

// LoadCatalog reads and parses the LSU catalog JSON.
func LoadCatalog(path string) (*LSUCatalog, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("LSU catalog 읽기 실패 (%s): %w", path, err)
	}
	var c LSUCatalog
	if err := json.Unmarshal(raw, &c); err != nil {
		return nil, fmt.Errorf("LSU catalog 파싱 실패 (%s): %w", path, err)
	}
	if c.DefaultLSU <= 0 {
		c.DefaultLSU = 50
	}
	if c.OvercommitFactor <= 0 {
		c.OvercommitFactor = 1.0 // 미지정/0 = 오버커밋 없음 (물리=광고)
	}
	return &c, nil
}

// LSUFor resolves the LSU value for a discovered GPU name.  It first tries an
// exact match, then a case-insensitive substring match (so "NVIDIA RTX A6000"
// from nvidia-smi matches a catalog key of the same model).  When nothing
// matches it falls back to DefaultLSU and reports that fact in source.
func (c *LSUCatalog) LSUFor(deviceName string) (lsu int, source string) {
	if d, ok := c.Devices[deviceName]; ok {
		return d.LSU, fmt.Sprintf("exact match (measured=%v)", d.Measured)
	}
	lower := strings.ToLower(deviceName)
	for name, d := range c.Devices {
		if strings.Contains(lower, strings.ToLower(name)) ||
			strings.Contains(strings.ToLower(name), lower) {
			return d.LSU, fmt.Sprintf("substring match '%s' (measured=%v)", name, d.Measured)
		}
	}
	return c.DefaultLSU, fmt.Sprintf("default_lsu (no catalog entry for %q)", deviceName)
}
