package main

// npu_proxy_cfg_test.go — 다중 프록시 구성 파서·검증 (Exp_59).
// GPU/NPU 하드웨어 불필요 (순수 구성 로직).

import (
	"errors"
	"testing"
)

const defDir = "/var/lib/kraken/npu"

func TestNPUProxyListSet(t *testing.T) {
	var l npuProxyList
	if err := l.Set("model=efficientnet_b0,pe=npu0pe1,dir=/var/lib/kraken/npu-eff"); err != nil {
		t.Fatalf("파싱 실패: %v", err)
	}
	if len(l) != 1 || l[0].Model != "efficientnet_b0" || l[0].PE != "npu0pe1" ||
		l[0].Dir != "/var/lib/kraken/npu-eff" {
		t.Fatalf("파싱 결과 불일치: %+v", l)
	}
	if err := l.Set("pe=npu0pe1"); err == nil {
		t.Fatal("model 누락인데 통과")
	}
	if err := l.Set("model=x,pe=y,bogus=z"); err == nil {
		t.Fatal("알 수 없는 키인데 통과")
	}
	if err := l.Set("nonsense"); err == nil {
		t.Fatal("key=value 아닌데 통과")
	}
}

func TestResolveLegacyOnly(t *testing.T) {
	// 하위호환: 기존 3플래그만 쓰면 종전과 동일한 단일 프록시.
	got, err := resolveNPUProxies("resnet50", "npu0pe0", defDir, nil)
	if err != nil {
		t.Fatalf("오류: %v", err)
	}
	if len(got) != 1 || got[0].Model != "resnet50" || got[0].PE != "npu0pe0" ||
		got[0].Dir != defDir {
		t.Fatalf("레거시 구성 불일치: %+v", got)
	}
}

func TestResolveMultiModel(t *testing.T) {
	extra := npuProxyList{{Model: "efficientnet_b0", PE: "npu0pe1",
		Dir: "/var/lib/kraken/npu-eff"}}
	got, err := resolveNPUProxies("resnet50", "npu0pe0", defDir, extra)
	if err != nil {
		t.Fatalf("오류: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("2개여야 함: %+v", got)
	}
	// 순서: 레거시가 0번(기존 소켓명 유지 = 하위호환)
	if got[0].Model != "resnet50" || got[1].Model != "efficientnet_b0" {
		t.Fatalf("순서 불일치: %+v", got)
	}
}

func TestResolveRejectsDuplicatePE(t *testing.T) {
	// ★이중계상 방어: 한 PE 가 두 모델에 지정되면 거부.
	extra := npuProxyList{{Model: "efficientnet_b0", PE: "npu0pe0", Dir: "/d2"}}
	if _, err := resolveNPUProxies("resnet50", "npu0pe0", defDir, extra); err == nil {
		t.Fatal("PE 중복인데 통과 — 이중계상 위험")
	}
}

func TestResolveRejectsDuplicateModel(t *testing.T) {
	extra := npuProxyList{{Model: "resnet50", PE: "npu0pe1", Dir: "/d2"}}
	if _, err := resolveNPUProxies("resnet50", "npu0pe0", defDir, extra); err == nil {
		t.Fatal("모델명 중복인데 통과 — 리소스 이름 충돌")
	}
}

func TestResolveRejectsSharedDir(t *testing.T) {
	// 프록시 2개가 같은 dir 를 쓰면 data.sock/admin.sock 충돌.
	extra := npuProxyList{{Model: "efficientnet_b0", PE: "npu0pe1", Dir: defDir}}
	if _, err := resolveNPUProxies("resnet50", "npu0pe0", defDir, extra); err == nil {
		t.Fatal("소켓 디렉토리 공유인데 통과")
	}
}

func TestModelPluginSocketNames(t *testing.T) {
	// 0번은 기존 소켓명 유지, 1번부터 모델별 접미 (라이브 재등록 노이즈 방지).
	p0 := NewNPUModelPluginN("resnet50", "npu0pe0", defDir, 0)
	p1 := NewNPUModelPluginN("efficientnet_b0", "npu0pe1", "/d2", 1)
	if p0.sockName != npuModelSocketName {
		t.Fatalf("0번 소켓명이 바뀜: %s", p0.sockName)
	}
	if p1.sockName == p0.sockName {
		t.Fatalf("1번이 0번과 같은 소켓 사용: %s", p1.sockName)
	}
	if p0.resourceName != "keti.io/npu-resnet50" ||
		p1.resourceName != "keti.io/npu-efficientnet_b0" {
		t.Fatalf("리소스명 불일치: %s / %s", p0.resourceName, p1.resourceName)
	}
	// 슬롯 ID 접두가 PE·모델을 담아 유일 (watcher 가 모델 구분 없이 추적 가능)
	if p0.deviceIDs[0] == p1.deviceIDs[0] {
		t.Fatalf("슬롯 ID 충돌: %s", p0.deviceIDs[0])
	}
}

func TestNPUPluginExcludesMultiplePEs(t *testing.T) {
	root := fakeWarboy(t)
	// 두 PE 다 프록시 → 직접 풀 0개 (다중 모델 트레이드오프의 코드적 근거)
	if _, err := NewNPUPlugin(root, "npu0pe0", "npu0pe1"); !errors.Is(err, ErrNoDirectPEs) {
		t.Fatalf("ErrNoDirectPEs 여야 함 (구성 오류 아님): %v", err)
	}
	// PE0 만 프록시 → PE1 은 직접 광고 (기존 구성)
	p2, err := NewNPUPlugin(root, "npu0pe0")
	if err != nil {
		t.Fatalf("오류: %v", err)
	}
	if len(p2.devices) != 1 {
		t.Fatalf("직접 풀 1개여야 함: %+v", p2.devices)
	}
}
