package main

// rngd_occupancy.go — [Exp_122 4부] 가용 PE 동적 노출.
//
// 문제: ListAndWatch 가 PE 8개를 **고정 광고**한다. 4PE 가 점유 중이어도 8이 노출돼
//   실제 가용량과 어긋난다. 그 상태로 8PE 를 요청한 파드는 배치는 되지만
//   컨테이너 안에서 EBUSY 로 죽는다(Exp_122 2-B: 거절까지 5.6초 — 늦게 온다).
//
// ★점유 확인 경로 = `furiosa-smi ps` 를 주 경로로, 우리 Allocate 기록을 보조로 합집합.
//   근거:
//     (ㄱ) `furiosa-smi ps` 만이 **외부 점유**를 본다. 다른 팀이 NPU 를 쓰면 여기 잡힌다.
//          우리 배정 기록만으로는 우리 밖을 못 본다(4부 요구사항).
//     (ㄴ) 그런데 ps 에는 **공백 구간**이 있다 — kubelet 이 파드를 배치한 순간부터
//          컨테이너 안 프로세스가 PE 를 열 때까지 5~8초(2-B/3-A 실측 로드 시간).
//          그 사이 두 번째 파드가 같은 블록에 배치될 수 있다. 우리 배정 기록이 메꾼다.
//     (ㄷ) 장치 노드 존재 여부는 점유를 나타내지 않는다 — 항상 존재한다. 쓰지 않는다.
//     (ㄹ) Core Utilization 은 부하 중에도 0.00% 로 나온다(짧은 커널 미포착). 쓰지 않는다.
//
// 표현 방식: 점유된 PE 를 Unhealthy 로 보내 kubelet 이 allocatable 에서 뺀다
//   (device-plugin API 의 표준 수단 — 개수를 직접 줄이는 경로는 없다).

import (
	"bufio"
	"context"
	"fmt"
	"log"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// smiPSLine: 범위 "| 1940701 | npu1:[0, 3] | cmd |" 와 **단일** "| 3072969 | npu0:0 | cmd |"
//   둘 다 매치한다. ★Exp_156 1부(A 음성 대조)에서 실측: 구 정규식은 범위 형식만 잡아
//   단일 PE 외부 점유가 광고에 **반영되지 않았다**(1PE 점유 중에도 8/8 광고 — 그 PE 를
//   내주면 충돌). 시험이 검사 결함이 아니라 광고 계층 결함을 잡은 사례.
var smiPSLine = regexp.MustCompile(`\|\s*(\d+)\s*\|\s*npu(\d+):(?:\[\s*(\d+)\s*,\s*(\d+)\s*\]|(\d+))\s*\|`)

// PEOccupancy 는 한 시점의 PE 점유 상태다.
type PEOccupancy struct {
	// key = "npu{idx}/pe{idx}" → 점유 사유
	Busy   map[string]string
	At     time.Time
	TookMS int64
	Err    error
}

func peKey(npuIdx, peIdx int) string { return fmt.Sprintf("npu%d/pe%d", npuIdx, peIdx) }

// scanSMIPS 는 `furiosa-smi ps` 로 실제 점유 PE 를 읽는다.
//   ★실패를 조용히 '점유 없음' 으로 바꾸지 않는다(Exp_107 T-5). Err 를 채워 올린다.
//   ★반드시 시한을 건다 — 시한 없는 외부 호출은 ListAndWatch 루프를 통째로 얼린다.
//     실측(Exp_122 4부 1차): hostPID 적용 후 `cmd.Output()` 이 돌아오지 않아 갱신이
//     한 번도 발화하지 않았다. 크래시도 경고도 없어 "8 고정" 과 구분되지 않았다.
func scanSMIPS(timeout time.Duration) PEOccupancy {
	occ := PEOccupancy{Busy: map[string]string{}, At: time.Now()}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "furiosa-smi", "ps")
	cmd.Stdin = nil
	var buf, errBuf strings.Builder
	cmd.Stdout = &buf
	cmd.Stderr = &errBuf
	err := cmd.Run()
	occ.TookMS = time.Since(occ.At).Milliseconds()
	if ctx.Err() != nil {
		occ.Err = fmt.Errorf("furiosa-smi ps 시한 초과(%s)", timeout)
		return occ
	}
	if err != nil {
		occ.Err = fmt.Errorf("furiosa-smi ps 실패: %w (%s)", err,
			strings.TrimSpace(errBuf.String()))
		return occ
	}
	out := []byte(buf.String())
	sc := bufio.NewScanner(strings.NewReader(string(out)))
	for sc.Scan() {
		m := smiPSLine.FindStringSubmatch(sc.Text())
		if m == nil {
			continue
		}
		pid := m[1]
		npuIdx, _ := strconv.Atoi(m[2])
		var lo, hi int
		if m[5] != "" { // 단일 PE 형식 npuN:K
			lo, _ = strconv.Atoi(m[5])
			hi = lo
		} else { // 범위 형식 npuN:[a, b]
			lo, _ = strconv.Atoi(m[3])
			hi, _ = strconv.Atoi(m[4])
		}
		for pe := lo; pe <= hi; pe++ {
			occ.Busy[peKey(npuIdx, pe)] = "smi_ps:pid=" + pid
		}
	}
	return occ
}

// reservations 는 우리가 Allocate 로 내준 블록이다. ps 의 공백 구간을 메꾼다.
type reservations struct {
	mu sync.Mutex
	// deviceID → 만료 시각. 컨테이너가 실제로 PE 를 열면 ps 가 잡으므로
	// 예약은 그때까지만 유효하면 된다. 만료를 두는 이유 — 파드가 기동에
	// 실패하면 예약이 영구히 남아 자원이 새기 때문이다.
	held map[string]time.Time
	ttl  time.Duration
}

func newReservations(ttl time.Duration) *reservations {
	return &reservations{held: map[string]time.Time{}, ttl: ttl}
}

func (r *reservations) add(ids []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	exp := time.Now().Add(r.ttl)
	for _, id := range ids {
		r.held[id] = exp
	}
}

func (r *reservations) active() map[string]bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := time.Now()
	out := map[string]bool{}
	for id, exp := range r.held {
		if now.After(exp) {
			delete(r.held, id)
			continue
		}
		out[id] = true
	}
	return out
}

// healthOf 는 device ID 별 Health 를 계산한다.
//   ★smi 실패 시에는 **아무 것도 Unhealthy 로 만들지 않고 경고만 한다.**
//     근거: 관측 실패를 '전량 점유' 로 해석하면 노드의 NPU 가 통째로 사라진다.
//     반대로 '전량 유휴' 로 해석하는 것도 위험하지만, 예약 기록은 여전히 살아 있어
//     우리 경로의 이중 배치는 막힌다. 어느 쪽이든 **조용히 넘어가지 않는다.**
func (p *RNGDPlugin) healthOf(occ PEOccupancy, resv map[string]bool) map[string]string {
	h := map[string]string{}
	for id, d := range p.devices {
		switch {
		case occ.Err == nil && occ.Busy[peKey(d.NpuIdx, d.PEIdx)] != "":
			h[id] = occ.Busy[peKey(d.NpuIdx, d.PEIdx)]
		case resv[id]:
			h[id] = "reserved:우리 Allocate"
		default:
			h[id] = ""
		}
	}
	return h
}

var rngdWarnOnce sync.Map

func rngdWarn(key, msg string) {
	if _, dup := rngdWarnOnce.LoadOrStore(key, true); !dup {
		log.Printf("[rngd][경고] %s", msg)
	}
}
