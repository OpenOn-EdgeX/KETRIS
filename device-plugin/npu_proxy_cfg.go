package main

// npu_proxy_cfg.go — 다중 프록시 PE 구성 (Exp_59 Phase 2-1/2-2).
//
// 배경: Warboy 1기 = PE 2개. Exp_47 구성은 PE0=프록시(모델 1종)+PE1=직접 경로
// (`keti.io/npu:1`). 모델을 늘리려면 PE1 도 프록시로 전환해야 하고 그 대가로
// **직접 경로를 잃는다** — 물리 PE 총량이 2 로 고정이기 때문(하드웨어 제약).
// 본 파일은 그 선택을 CLI 로 표현 가능하게 한다: 어느 PE 를 어느 모델의
// 프록시로 쓸지 나열하면, 남은 PE 만 직접 풀로 광고된다.
//
// 이중계상 방어: 프록시로 지정된 PE 는 전부 직접 풀에서 제외(excludePEs)되고,
// 같은 PE 를 두 모델에 지정하면 구성 오류로 기동을 거부한다 — 한 PE 가 두
// 리소스 이름으로 계상되면 물리 초과 배치가 된다 (Exp_31 재발 금지).
//
// 다중 NPU(npu1, npu2…): PE ID 문자열이 npu<N>pe<M> 형식이라 본 구성은 NPU
// 인덱스에 대해 이미 일반적이다. 다만 232 는 Warboy 1기뿐이라 **다중 NPU 는
// 실측 불가**이며, 검증 없는 추가 추상화(NPU 별 프록시 자동 배치 등)는 넣지
// 않는다 (Exp_59 §2-2 판단).

import (
	"fmt"
	"strings"
)

// NPUProxySpec is one proxy-owned PE serving one model.
type NPUProxySpec struct {
	Model string
	PE    string
	Dir   string
}

// npuProxyList implements flag.Value for the repeatable --npu-proxy flag.
type npuProxyList []NPUProxySpec

func (l *npuProxyList) String() string {
	parts := make([]string, 0, len(*l))
	for _, s := range *l {
		parts = append(parts, s.Model+"@"+s.PE)
	}
	return strings.Join(parts, " ")
}

// Set parses "model=<m>,pe=<pe>[,dir=<path>]".
func (l *npuProxyList) Set(v string) error {
	spec := NPUProxySpec{}
	for _, kv := range strings.Split(v, ",") {
		kv = strings.TrimSpace(kv)
		if kv == "" {
			continue
		}
		k, val, ok := strings.Cut(kv, "=")
		if !ok {
			return fmt.Errorf("항목 %q: key=value 형식이어야 함", kv)
		}
		switch strings.TrimSpace(k) {
		case "model":
			spec.Model = strings.TrimSpace(val)
		case "pe":
			spec.PE = strings.TrimSpace(val)
		case "dir":
			spec.Dir = strings.TrimSpace(val)
		default:
			return fmt.Errorf("알 수 없는 키 %q (model|pe|dir)", k)
		}
	}
	if spec.Model == "" || spec.PE == "" {
		return fmt.Errorf("model 과 pe 는 필수 (%q)", v)
	}
	*l = append(*l, spec)
	return nil
}

// resolveNPUProxies merges the legacy single-proxy flags with the repeatable
// --npu-proxy list and validates the result.
//
// 반환 순서 = 광고·소켓 인덱스 순서(0번은 기존 소켓명 유지 = 하위호환).
// 레거시 플래그(--npu-proxy-model)가 있으면 항상 0번이 된다.
func resolveNPUProxies(legacyModel, legacyPE, defaultDir string,
	extra npuProxyList) ([]NPUProxySpec, error) {
	out := make([]NPUProxySpec, 0, len(extra)+1)
	if legacyModel != "" {
		out = append(out, NPUProxySpec{Model: legacyModel, PE: legacyPE, Dir: defaultDir})
	}
	for _, s := range extra {
		if s.Dir == "" {
			s.Dir = defaultDir
		}
		out = append(out, s)
	}

	seenPE := map[string]string{}    // PE → model
	seenModel := map[string]string{} // model → PE
	for _, s := range out {
		if prev, dup := seenPE[s.PE]; dup {
			return nil, fmt.Errorf("PE %s 가 모델 %s 와 %s 에 중복 지정 — "+
				"한 PE 는 한 모델만 (이중계상 방지)", s.PE, prev, s.Model)
		}
		if prev, dup := seenModel[s.Model]; dup {
			return nil, fmt.Errorf("모델 %s 가 PE %s 와 %s 에 중복 지정 — "+
				"리소스 이름(keti.io/npu-%s)이 충돌", s.Model, prev, s.PE, s.Model)
		}
		if s.Dir == "" {
			return nil, fmt.Errorf("모델 %s: 프록시 소켓 디렉토리(dir)가 비었음", s.Model)
		}
		if prev, dup := seenDir(out, s); dup {
			return nil, fmt.Errorf("모델 %s 와 %s 가 같은 소켓 디렉토리(%s) 사용 — "+
				"프록시별로 분리해야 data.sock/admin.sock 이 충돌하지 않음",
				prev, s.Model, s.Dir)
		}
		seenPE[s.PE] = s.Model
		seenModel[s.Model] = s.PE
	}
	return out, nil
}

// seenDir reports whether another spec already claims s.Dir.
func seenDir(all []NPUProxySpec, s NPUProxySpec) (string, bool) {
	for _, o := range all {
		if o.Model != s.Model && o.Dir == s.Dir {
			return o.Model, true
		}
	}
	return "", false
}
