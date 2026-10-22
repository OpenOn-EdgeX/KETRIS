package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

var errRetiringOwner = errors.New("share owner has not retired yet")

type Binding struct {
	SchemaVersion int      `json:"schema_version"`
	AllocationID  string   `json:"allocation_id"`
	Node          string   `json:"node"`
	GPUUUID       string   `json:"gpu_uuid"`
	MIGUUID       string   `json:"mig_uuid"`
	Generation    string   `json:"generation"`
	ResourceName  string   `json:"resource_name"`
	DeviceIDs     []string `json:"device_ids"`
	SpacePercent  int      `json:"space_percent"`
	ControlDir    string   `json:"control_dir"`
	Phase         string   `json:"phase"`
	UpdatedAt     string   `json:"updated_at"`
}

func atomicJSON(path string, value any) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".pending-")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if err = json.NewEncoder(f).Encode(value); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(tmp, path); err != nil {
		return err
	}
	parent, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer parent.Close()
	return parent.Sync()
}

func sameIDs(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	a = append([]string{}, a...)
	b = append([]string{}, b...)
	sort.Strings(a)
	sort.Strings(b)
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func (p *Plugin) lease(ids []string) (Binding, error) {
	paths, err := filepath.Glob(filepath.Join(p.Config.StateDir, "allocations", "*.json"))
	if err != nil {
		return Binding{}, err
	}
	wanted := map[string]bool{}
	for _, id := range ids {
		wanted[id] = true
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return Binding{}, err
		}
		var b Binding
		if err = json.Unmarshal(raw, &b); err != nil {
			return Binding{}, fmt.Errorf("corrupt allocation journal: %w", err)
		}
		if b.MIGUUID != p.Config.MIGUUID || b.Generation != p.Config.Generation {
			continue
		}
		if b.SchemaVersion != 1 || b.Node != p.Config.Node || b.GPUUUID != p.Config.GPUUUID || b.ResourceName != p.Config.ResourceName || b.SpacePercent != len(b.DeviceIDs) || b.SpacePercent < 1 || b.SpacePercent > 100 || b.ControlDir != filepath.Join(p.Config.StateDir, "control", b.AllocationID) || b.Phase != "allocated" {
			return Binding{}, fmt.Errorf("allocation journal identity/cap is invalid")
		}
		if _, err = os.Stat(filepath.Join(p.Config.StateDir, "tombstones", b.AllocationID)); err == nil {
			continue
		} else if !os.IsNotExist(err) {
			return Binding{}, err
		}
		owned := false
		if raw, err := os.ReadFile(filepath.Join(p.Config.StateDir, "bindings", b.AllocationID+".json")); err == nil {
			var state struct {
				PodUID string `json:"pod_uid"`
			}
			if err = json.Unmarshal(raw, &state); err != nil {
				return Binding{}, fmt.Errorf("corrupt binding state: %w", err)
			}
			owned = state.PodUID != ""
		} else if !os.IsNotExist(err) {
			return Binding{}, err
		}
		if sameIDs(ids, b.DeviceIDs) {
			if owned {
				return Binding{}, errRetiringOwner
			}
			return b, nil
		}
		for _, id := range b.DeviceIDs {
			if wanted[id] {
				if owned {
					return Binding{}, errRetiringOwner
				}
				return Binding{}, fmt.Errorf("shared unit already belongs to an active allocation")
			}
		}
	}
	var bytes [12]byte
	if _, err = rand.Read(bytes[:]); err != nil {
		return Binding{}, err
	}
	id := "a-" + hex.EncodeToString(bytes[:])
	control := filepath.Join(p.Config.StateDir, "control", id)
	if len(filepath.Join(control, "bless-1.sock")) >= 108 {
		return Binding{}, fmt.Errorf("control socket path exceeds Unix domain socket limit; use short node state directory")
	}
	if err = os.MkdirAll(control, 0700); err != nil {
		return Binding{}, err
	}
	b := Binding{1, id, p.Config.Node, p.Config.GPUUUID, p.Config.MIGUUID, p.Config.Generation, p.Config.ResourceName, append([]string{}, ids...), len(ids), control, "allocated", time.Now().UTC().Format(time.RFC3339Nano)}
	if err = atomicJSON(filepath.Join(p.Config.StateDir, "allocations", id+".json"), b); err != nil {
		os.RemoveAll(control)
		return Binding{}, err
	}
	return b, nil
}
