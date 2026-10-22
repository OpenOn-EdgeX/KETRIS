package main

import (
	"encoding/csv"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
)

// GPUInfo holds information about a physical GPU discovered via nvidia-smi.
type GPUInfo struct {
	Index    int
	Name     string
	MemoryMB int64
	// UUID identifies the GPU for CDI device grant (Exp_71 —
	// "nvidia.com/gpu=<UUID>"). 다중 GPU 에서 =all 과 달리 해당 GPU 만 한정.
	UUID       string
	MIGCapable bool
	// SmCount is populated after querying the KRAKEN controller.
	SmCount int32
	// LSU is the Logical Slice Unit value for this GPU model, resolved from
	// lsu/catalog.json in LSU mode.  It is the number of logical units this
	// GPU advertises to Kubernetes.
	LSU int
	// LSUSource records how LSU was resolved (for logging/credibility).
	LSUSource string
}

// DiscoverGPUs enumerates physical GPUs using nvidia-smi.
// Returns an error if nvidia-smi is unavailable or no GPUs are found.
func DiscoverGPUs() ([]GPUInfo, error) {
	out, err := exec.Command(
		"nvidia-smi",
		"--query-gpu=index,name,memory.total,uuid,mig.mode.current",
		"--format=csv,noheader,nounits",
	).Output()
	if err != nil {
		return nil, fmt.Errorf("nvidia-smi 실패: %w", err)
	}

	var gpus []GPUInfo
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		// Fields: "0, NVIDIA A100-SXM4-80GB, 81920, GPU-xxxx"
		reader := csv.NewReader(strings.NewReader(line))
		reader.TrimLeadingSpace = true
		parts, parseErr := reader.Read()
		if parseErr != nil || len(parts) != 5 {
			return nil, fmt.Errorf("nvidia-smi 출력 파싱 실패: %q", line)
		}

		idx, err := strconv.Atoi(strings.TrimSpace(parts[0]))
		if err != nil {
			return nil, fmt.Errorf("GPU index 파싱 실패 %q: %w", parts[0], err)
		}
		memMB, err := strconv.ParseInt(strings.TrimSpace(parts[2]), 10, 64)
		if err != nil {
			return nil, fmt.Errorf("메모리 파싱 실패 %q: %w", parts[2], err)
		}

		migMode := strings.TrimSpace(parts[4])
		if migMode == "Enabled" {
			continue
		}
		if migMode != "Disabled" && migMode != "N/A" && migMode != "[N/A]" && migMode != "[Not Supported]" {
			return nil, fmt.Errorf("unknown MIG mode %q; refusing full-GPU advertisement", migMode)
		}
		gpus = append(gpus, GPUInfo{
			Index:      idx,
			Name:       strings.TrimSpace(parts[1]),
			MemoryMB:   memMB,
			UUID:       strings.TrimSpace(parts[3]),
			MIGCapable: migMode == "Disabled",
		})
	}

	return gpus, nil
}

// IsGPUHealthy returns true when nvidia-smi can query the given GPU index
// without errors.  A non-zero exit code or an "Error" token in the output
// means the GPU is unhealthy.
func IsGPUHealthy(idx int) bool {
	out, err := exec.Command(
		"nvidia-smi",
		"-i", strconv.Itoa(idx),
		"--query-gpu=index,mig.mode.current",
		"--format=csv,noheader,nounits",
	).Output()
	if err != nil {
		return false
	}
	parts := strings.Split(strings.TrimSpace(string(out)), ",")
	if len(parts) != 2 {
		return false
	}
	mode := strings.TrimSpace(parts[1])
	return mode == "Disabled" || mode == "N/A" || mode == "[N/A]" || mode == "[Not Supported]"
}
