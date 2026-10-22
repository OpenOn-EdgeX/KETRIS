package main

// bless_feeder.go — GPU 테넌트 시간축 자동 배선 (Exp_73 Part B).
//
// 문제: libbless 제어 소켓은 파드 기동 후에야 생기므로(Allocate 시점엔 없음)
//   GPU Allocate 경로가 feeder 에 등록할 수 없었다 → 라이브 시간 축이 계속 꺼짐.
// 해결: blessSockDir 아래 <devID>/bless-1.sock 을 주기 스캔해 reconcile —
//   ★새 소켓 = feeder register+arm, ★사라진 소켓 = deregister(stale 누적 방지,
//   Exp_53 위생 계보). 감지 주체를 watcher(소켓 생성 관측)로 둔 이유: Allocate 는
//   너무 이르고, 파드 PID 네임스페이스에서 libbless 가 pid 1 로 bless-1.sock 을
//   결정적으로 만든다(Exp_72 실측).

import (
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// blessFeederWirer reconciles bless control sockets → feeder registration.
// [Exp_107 T-2 / Exp_110 R-1] class 미도착 허용 틱 수.
//   ★Exp_110 에서 watcher 가 파드 env 로부터 class 를 **선기록**하므로 정상 경로에서는
//     이 유예가 발동하지 않는다. 다만 **제거하지 않고 폴백으로 남긴다** — 이유:
//       ① watcher 가 checkpoint 매핑을 아직 못 본 순간(파드 Added 직후)에는 선기록이
//          미뤄지고, 그때는 libbless init 을 기다려야 한다
//       ② env 를 선언하지 않은 파드는 선기록 대상이 아니다(미선언=strict 유지)
//       ③ watcher 가 죽거나 RBAC 이 없는 환경에서도 동작해야 한다
//     즉 선기록은 **빠른 경로**이고 이 유예는 **느린 경로**다. 둘 다 필요하다.
//
//   ★값 15→5 (Exp_110 R-1 실측 근거). 15틱=30초는 축 관측 창(첫 파드 기준 22초)보다
//     길어서, class 를 끝내 못 받는 테넌트가 하나라도 섞이면 relaxed 판정이 창 안에
//     내려지지 못했다 — 이것이 짝짓기 간헐 실패의 실제 기전이다(Exp_109 의 "init 지연"
//     가설은 부분 원인일 뿐이었다). 선기록이 붙은 지금 class 는 Allocate 후 ≤5초에
//     도착하므로(체크포인트 동기 주기 5초) 5틱=10초면 충분하고, 관측 창 안에서
//     유예가 만료돼 경고와 함께 판정이 진행된다.
//   libbless init 이 CUDA 컨텍스트 생성 직후 class 를 쓰므로, 모델 로드가 긴
//   워크로드(bert/llm)까지 감안해 15틱으로 둔다 — 현재 주기 2s 기준 ≈30s.
//   ★폴링 주기를 줄이는 방식은 택하지 않았다 — 부하만 늘고 경합 자체는 남는다.
const classGraceTicks = 5

// [Exp_138 2-C] 관측 진동 방지 — Exp_121 재분배의 값을 그대로 재사용한다.
//   obsHoldTicks 5  = KRAKEN_REDIST_HOLD 기본값(연속 확인 횟수)
//   obsDwellTicks 10 = KRAKEN_REDIST_DWELL 기본값(전환 후 최소 유지 구간)
//   ★새 상수를 만들지 않는다. 두 값 모두 Exp_121 에서 진동 없이 수렴한 실적이 있다.
const obsHoldTicks = 5
const obsDwellTicks = 10

type blessFeederWirer struct {
	sockDir string
	ctrl    *ControllerClient
	// units(devID) → (요청 LSU, 살아있음). Exp_74: stale 배제 + LSU 비례 ratio.
	units func(devID string) (int, bool)
	// physLSU (Exp_77) — 물리 LSU. ratio = 요청LSU/물리LSU (분수 계약 복원,
	// time_sharing.md §4). 0 이면 안전상 1(분수화 안 함=구 동작).
	physLSU    int
	interval   time.Duration
	stopCh     chan struct{}
	wg         sync.WaitGroup
	registered map[string]int  // devID → 등록에 쓴 ratio(=units)
	armed      map[string]bool // devID → 현재 arm 상태(쌍 판정 결과)
	// [Exp_107 T-2] devID → class 미도착 연속 틱 수. classGraceTicks 초과 시 경고 후 진행.
	classWait map[string]int
	// [Exp_110 R-1] 유예를 이미 소진한 devID. 한 번 포기했으면 다시 붙들지 않는다 —
	//   class 를 아예 선언하지 않는 파드(시간 축 등)가 12초 보류 → 경고 → 재보류를
	//   무한 반복해 relaxed 판정이 대부분의 틱에서 내려지지 않았다(실측 로그).
	classGaveUp map[string]bool
	// [Exp_138 2-C] 관측 진동 방지 — ★새 상수를 만들지 않고 Exp_121 재분배의
	//   3겹(HOLD 5 · DWELL 10)을 그대로 재사용한다. 창(25 tick)은 스캐너가 이미
	//   게이트(3.0s)로 대신하므로 옮기지 않는다.
	//   obsSeen  : devID → (마지막으로 본 관측값, 연속 관측 횟수)
	//   lastSwitch: devID → 마지막 모드 전환 시각(틱). DWELL 이내면 바꾸지 않는다.
	obsSeen    map[string]string
	obsHold    map[string]int
	lastSwitch map[string]int64
	tick       int64
	// [Exp_138 2-D / T-7] 관측 class 기록 자리에 선언 class 와 같은 위생을 옮긴다.
	//   effReason: devID → 마지막 유효 class 사유(이벤트 중복 발행 억제)
	effReason map[string]string
	// [Exp_112] devID → 등록에 쓴 소켓 경로. 경로가 바뀌면 재등록한다.
	//   ★근본 원인(실측): devID 는 재사용되고 종료된 파드의 bless-*.sock 파일이 디렉터리에
	//     남는다. Allocate 직후(새 파드의 libbless 가 아직 소켓을 만들기 전) 스캔이 돌면
	//     **죽은 소켓으로 등록**된다. 이후 units 가 그대로라 재등록이 일어나지 않아
	//     time_stats 명령이 영원히 허공으로 가고 charged 가 0 에 머문다
	//     → occupancy 한쪽이 0 → 시간 축 "비=0" (Exp_110 미규명 현상의 정체).
	registeredSock map[string]string
}

// ── [Exp_138 2부] Deep Scanner 관측 분류 연결 ────────────────────────────────
//
// ★기본 꺼짐. KRAKEN_SCANNER_LINK=1 로 켠다. 꺼지면 obs_class 를 읽지 않으므로
//   기존 동작과 한 비트도 다르지 않다.
//
// 관측 class 는 controller/scanner/scan_loop.py 가 <sockDir>/<devID>/obs_class 에
// 쓴다. 그 파일은 **표본 충분성 게이트**(경과 3.0s AND 커널 180개, Exp_138 1-A 유도)를
// 통과한 뒤에만 생긴다 — 통과 전에는 파일이 없고, 없으면 선언만 본다.
var scannerLink = os.Getenv("KRAKEN_SCANNER_LINK") == "1"

// readObsClass reads the scanner's observed classification.
// 미생성/미지원 값 = "" → 선언 경로 그대로.
func readObsClass(dir string) string {
	if !scannerLink {
		return ""
	}
	b, err := os.ReadFile(filepath.Join(dir, "obs_class"))
	if err != nil {
		return ""
	}
	c := strings.TrimSpace(string(b))
	switch c {
	case "COMPUTE_BOUND", "MEMORY_BOUND", "MIXED":
		return c
	}
	return "" // UNKNOWN 포함 — 근거 없음으로 취급
}

// effectiveClass merges declaration and observation (Exp_138 설계 전제 3).
//
// ★관측은 **안전한 방향으로만** 선언을 뒤집는다. 게이트를 떼는 쪽이 위험 방향이다 —
//   연산 집약 작업을 풀어주면 독차지해 옆 작업의 계약 몫이 깨진다(Exp_89 게이트없음
//   유지율 0.477). 잘못 거는 것은 이득만 잃는다(대칭 0.359 vs 배치측만 0.438).
//
//   선언 memory + 관측 COMPUTE_BOUND → 중립("")으로 후퇴 = 그 테넌트는 arm 된다
//   선언 compute + 관측 무엇이든     → 유지 (떼지 않는다). 불일치면 사유만 남긴다
//   미선언       + 관측              → 2-A 판정표대로
//
// 반환: (유효 class, 사유). 사유는 로그·이벤트에 실린다.
func effectiveClass(decl, obs string) (string, string) {
	if obs == "" {
		return decl, "선언" // 게이트 통과 전 = 지금과 같음
	}
	// 2-A 판정표: 풀어줄 후보 칸 — 근거는 Exp_39b prefill×decode(관측 COMPUTE×MIXED)
	//   0.72 → 1.09. ★그 근거는 하네스 워크로드에 한정된다(Exp_138 1-D).
	obsVictim := obs == "MEMORY_BOUND" || obs == "MIXED"
	switch decl {
	case "memory":
		if obs == "COMPUTE_BOUND" {
			return "", "불일치-후퇴" // 안전 방향: victim 자격 박탈 → 대칭
		}
		return decl, "선언-일치"
	case "compute":
		if obsVictim {
			return decl, "불일치-유지" // 떼지 않는다(전제 3). 이벤트만
		}
		return decl, "선언-일치"
	default: // 미선언
		if obsVictim {
			return "memory", "관측"
		}
		if obs == "COMPUTE_BOUND" {
			return "compute", "관측"
		}
		return "", "관측-근거없음"
	}
}

// relaxedDecision computes per-tenant arm state by the pair-wise rule
// (Exp_75 A-0: 격리는 aggressor 에게만, victim 은 풀어준다). 반환: devID→arm(=gate).
//   · aggressor(compute) 와 victim(memory) 가 ★공존할 때만 victim 을 해제(arm=false).
//   · 동종(전부 compute 또는 전부 memory) / 미선언 혼재 → 전원 arm(strict, 안전).
//     이유: memory 만 둘이면 양쪽 해제 = 몫 제어 소멸(A-0 금지).
func relaxedDecision(classes map[string]string) map[string]bool {
	hasAgg, hasVic := false, false
	for _, c := range classes {
		if c == "compute" {
			hasAgg = true
		} else if c == "memory" {
			hasVic = true
		}
	}
	relaxed := hasAgg && hasVic
	out := make(map[string]bool, len(classes))
	for id, c := range classes {
		// relaxed 성립 && 이 테넌트가 victim(memory) → 해제. 그 외 전부 arm.
		out[id] = !(relaxed && c == "memory")
	}
	return out
}

func newBlessFeederWirer(sockDir string, ctrl *ControllerClient,
	units func(string) (int, bool), physLSU int) *blessFeederWirer {
	return &blessFeederWirer{
		sockDir:    sockDir,
		ctrl:       ctrl,
		units:      units,
		physLSU:    physLSU,
		interval:   feederScanInterval,
		stopCh:     make(chan struct{}),
		registered: make(map[string]int),
		armed:      make(map[string]bool),
		classWait:  make(map[string]int),
		classGaveUp: make(map[string]bool),
		registeredSock: make(map[string]string),
		obsSeen:    make(map[string]string),
		obsHold:    make(map[string]int),
		lastSwitch: make(map[string]int64),
		effReason:  make(map[string]string),
	}
}

// scanned holds a live tenant's socket path, requested LSU (ratio), and
// declared workload class (Exp_75 — "compute"|"memory"|""(미선언)).
type scanned struct {
	sock  string
	units int
	class string
	obs   string // [Exp_138] 스캐너 관측 분류 (게이트 통과 후에만 채워진다)
}

// readClass reads the declared workload class file (libbless BLESS_CLASS_LOG).
// 미선언/미기록 = "" → 안전 기본값 strict 로 이어진다.
func readClass(dir string) string {
	b, err := os.ReadFile(filepath.Join(dir, "class"))
	if err != nil {
		return ""
	}
	c := strings.TrimSpace(string(b))
	if c == "compute" || c == "memory" {
		return c
	}
	return ""
}

// scan returns live devIDs (할당됨 + 소켓 존재) with their requested LSU.
func (w *blessFeederWirer) scan() map[string]scanned {
	out := map[string]scanned{}
	entries, err := os.ReadDir(w.sockDir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		devID := e.Name()
		// ★stale 배제: 소켓 파일은 파드 사망 후에도 디스크에 남는다. 현재 할당된
		// 테넌트만 등록 대상. units==0 이면 무효.
		u, live := 0, true
		if w.units != nil {
			u, live = w.units(devID)
		}
		if !live || u <= 0 {
			// [Exp_153 4부] 할당 해제된 devID 의 잔재 소켓 회수 — 여기가 실제
			// 잔재가 쌓이는 자리다(파드 종료 → units 0 → 스캔 제외 → 파일 잔존).
			reapDeadSocks(filepath.Join(w.sockDir, devID))
			continue
		}
		// ★Exp_76: bless-*.sock glob. libbless 소켓명은 컨테이너 PID(getpid())로
		// 결정 — 워크로드가 pid 1 이 아니면(예: `bash -c "python ..."` 에서 python
		// 은 bash 의 자식) bless-1.sock 이 아니다. bless-1.sock 하드코딩은 그런
		// 워크로드(실 추론 등)를 놓쳐 무게이트가 됐다. 소켓 파일 아무거나 매칭.
		if sock := findBlessSock(filepath.Join(w.sockDir, devID)); sock != "" {
			out[devID] = scanned{sock: sock, units: u,
				class: readClass(filepath.Join(w.sockDir, devID)),
				obs:   readObsClass(filepath.Join(w.sockDir, devID))}
		}
	}
	return out
}

// ratioOf converts requested LSU to the feeder's fractional ratio (Exp_77
// 계약 복원): ratio = 요청LSU / 물리LSU. strict(Σreq=물리) → Σratio=1.0 → 각자 몫.
// relaxed(aggressor만 armed) → Σ<1 → denom 하한 1.0 → aggressor 가 그 분수로 스로틀.
// 오버커밋(Σreq>물리) → Σratio>1 → 비례 정규화(시분할 흡수). physLSU<=0 이면 구 동작.
func (w *blessFeederWirer) ratioOf(units int) float64 {
	if w.physLSU > 0 {
		return float64(units) / float64(w.physLSU)
	}
	return float64(units)
}

// sockAlive reports whether a unix datagram socket file has a live listener.
//
//   ★필요한 이유(Exp_112 실측): devID 는 재사용되고 종료된 파드의 bless-<pid>.sock
//     파일이 디렉터리에 남는다. mtime 만 보면 **죽은 소켓에 등록**되고, 그 뒤 보낸
//     arm/credit 명령이 허공으로 간다. 파드는 mode=0 time_credit=-1 로 남아
//     charged 가 영구 0 이 되는데 feeder status 는 armed=true 로 보인다(거짓 양성).
//     파일명이 우연히 같으면 경로 비교로도 못 잡는다 → 생사를 직접 확인한다.
//   보내는 문자열은 libbless 가 모르는 명령이라 무시된다(핸들러 체인에 else 없음).
func sockAlive(path string) bool {
	c, err := net.Dial("unixgram", path)
	if err != nil {
		return false
	}
	defer c.Close()
	_, err = c.Write([]byte("ping"))
	return err == nil // 리스너 없으면 ECONNREFUSED
}

// staleSockGrace: 죽은 소켓 파일을 지우기 전 유예. classGraceTicks(5틱 — libbless
// init 이 소켓을 만들고 class 를 쓰기까지의 시간 규모, Exp_107)와 같은 근거를 재사용
// 한다 — bind 직후 판정 흔들림(TOCTOU)을 mtime 유예로 배제한다.
const staleSockGraceTicks = classGraceTicks

// feederScanInterval: 스캔 폴링 주기 — wirer.interval 과 잔재 소켓 유예 계산이
// 같은 값을 쓰도록 상수로 뽑았다(Exp_153 4부).
const feederScanInterval = 2 * time.Second

// reapDeadSocks removes dead bless-*.sock files in dir.
//
// [Exp_153 4부] 죽은 잔재 소켓은 **파일도 지운다** — Exp_147(16개)·151(119개)·
// 152(24개)를 손으로 지웠던 회수 경로의 자동화. 할당 중(findBlessSock 경유)과
// **할당 해제 후(scan 의 units<=0 경로 — 실제 잔재가 쌓이는 자리)** 둘 다 돈다.
// 안전 논거:
//   · 삭제 대상 = sockAlive false(리스너 없음 확정) **그리고** mtime 이
//     staleSockGrace 이전. 새 파드의 소켓은 bind 가 파일을 만들므로 살아 있고
//     (sockAlive true), devID 재사용이어도 새 파드는 자기 pid 이름으로 새 파일을
//     만든다(Exp_112) — 같은 파일을 지울 일이 없다. 같은 pid 재사용으로 파일명이
//     겹치는 극단 경우도 그 파일이 살아 있으면 안 지워짐.
//   · mtime 유예: bind 직후 잠깐 dial 이 흔들려도(이론상) 갓 만든 파일은 안 지운다.
func reapDeadSocks(dir string) {
	matches, _ := filepath.Glob(filepath.Join(dir, "bless-*.sock"))
	graceAgo := time.Now().Add(-time.Duration(staleSockGraceTicks) * feederScanInterval)
	for _, p := range matches {
		fi, err := os.Stat(p)
		if err != nil || fi.Mode()&os.ModeSocket == 0 {
			continue
		}
		if !sockAlive(p) && fi.ModTime().Before(graceAgo) {
			if rmErr := os.Remove(p); rmErr == nil {
				log.Printf("[bless-feeder][Exp_153] 잔재 소켓 회수: %s (리스너 없음·유예 %d틱 경과)",
					p, staleSockGraceTicks)
			} else {
				log.Printf("[bless-feeder][경고] 잔재 소켓 삭제 실패 %s: %v", p, rmErr)
			}
		}
	}
}

// findBlessSock returns the newest **live** bless-*.sock in dir, or "".
func findBlessSock(dir string) string {
	reapDeadSocks(dir)
	matches, _ := filepath.Glob(filepath.Join(dir, "bless-*.sock"))
	var best string
	var bestT int64
	for _, p := range matches {
		fi, err := os.Stat(p)
		if err != nil || fi.Mode()&os.ModeSocket == 0 {
			continue
		}
		if !sockAlive(p) {
			continue // [Exp_112] 죽은 잔재 소켓 배제
		}
		if mt := fi.ModTime().UnixNano(); mt >= bestT { // 최신 소켓(재기동 대비)
			bestT = mt
			best = p
		}
	}
	return best
}

// reconcile registers newly-seen sockets and deregisters vanished ones.
// ★Exp_74: ratio = 요청 LSU. feeder 가 매 tick armed 집합에서 ratio_i/Σ 로
// 정규화하므로(Σt=1 완전배분), 테넌트 추가·제거 시 남은 테넌트 비율은 자동
// 재계산된다 — 등록 시 raw units 만 넣으면 된다. LSU 가 바뀌면(재할당) ratio 갱신.
func (w *blessFeederWirer) reconcile() {
	live := w.scan()
	// 1) 신규 등록 / ratio 갱신 (LSU 비례, ★분수 계약: ratio=요청LSU/물리LSU)
	for devID, s := range live {
		prev, known := w.registered[devID]
		logPath := filepath.Join(w.sockDir, devID, "feed.log")
		ratio := w.ratioOf(s.units)
		// [Exp_112] 소켓 경로가 바뀌었으면 units 가 같아도 재등록한다.
		if known && w.registeredSock[devID] != s.sock {
			if err := w.ctrl.FeederRegister(devID, s.sock, logPath, ratio, "gpu"); err != nil {
				log.Printf("[bless-feeder] 소켓 갱신 재등록 실패 %s: %v (재시도)", devID, err)
				continue
			}
			log.Printf("[bless-feeder] 소켓 갱신: %s %s → %s (죽은 소켓 선점 해소)",
				devID, filepath.Base(w.registeredSock[devID]), filepath.Base(s.sock))
			w.registeredSock[devID] = s.sock
			// ★재등록만으로는 부족하다. 죽은 소켓에 보냈던 arm/release 는 파드에
			//   도달하지 않았으므로 libbless 는 여전히 mode=0 time_credit=-1 이다
			//   (실측: status 는 armed=true 인데 target_share=None, charged 가 영구 0).
			//   상태 캐시를 지워 다음 판정이 arm/release 를 **다시 보내게** 한다.
			delete(w.armed, devID)
		}
		if !known {
			if err := w.ctrl.FeederRegister(devID, s.sock, logPath, ratio, "gpu"); err != nil {
				log.Printf("[bless-feeder] register 실패 %s: %v (재시도)", devID, err)
				continue
			}
			w.registeredSock[devID] = s.sock
			log.Printf("[bless-feeder] 자동 등록: %s ratio=%d class=%q sock=%s",
				devID, s.units, s.class, filepath.Base(s.sock))
		} else if prev != s.units {
			if err := w.ctrl.FeederRegister(devID, s.sock, logPath, ratio, "gpu"); err != nil {
				log.Printf("[bless-feeder] ratio 갱신 실패 %s: %v", devID, err)
				continue
			}
			w.registeredSock[devID] = s.sock
			log.Printf("[bless-feeder] ratio 갱신: %s %d→%d", devID, prev, s.units)
		}
		w.registered[devID] = s.units
	}
	// 2) ★쌍 판정 (Exp_75): 현재 공존 조합으로 arm/release 재계산. 조합이 바뀌면
	//    (테넌트 추가·제거) 매 스캔 재평가된다.
	//
	// [Exp_107 T-2] class 미도착 시 판정 보류.
	//   class 파일은 libbless init 이 쓰고 여기서는 폴링으로 읽으므로, 파드가 갓 뜬
	//   시점에는 비어 있을 수 있다. 그 상태로 판정하면 이종 쌍이 성립하지 않아
	//   victim 이 arm 으로 남는다(Exp_106 3-A: 6축 연속 실행에서 간헐 FAIL).
	//   → 빈 class 가 하나라도 있으면 이번 라운드 판정을 건너뛰고 다음 폴링을 기다린다.
	//   대기 상한 = 폴링 주기 × classGraceTicks. libbless init 은 CUDA 컨텍스트 생성
	//   직후 쓰므로 모델 로드가 긴 워크로드까지 감안해 5틱(≈30s)으로 잡는다.
	classes := make(map[string]string, len(live))
	reasons := make(map[string]string, len(live))
	pendingClass := 0
	w.tick++
	for devID, s := range live {
		// [Exp_138 2-C] 관측이 obsHoldTicks 연속으로 같아야 채택한다.
		obs := s.obs
		if obs != "" {
			if w.obsSeen[devID] == obs {
				w.obsHold[devID]++
			} else {
				w.obsSeen[devID] = obs
				w.obsHold[devID] = 1
			}
			if w.obsHold[devID] < obsHoldTicks {
				obs = "" // 아직 안정 미확인 — 선언만 본다
			}
		} else {
			delete(w.obsSeen, devID)
			delete(w.obsHold, devID)
		}
		eff, why := effectiveClass(s.class, obs)
		classes[devID] = eff
		reasons[devID] = why
		if scannerLink && w.effReason[devID] != why {
			w.effReason[devID] = why
			if why != "선언" && why != "선언-일치" {
				log.Printf("[bless-feeder][Exp_138] %s 유효class=%q 사유=%s "+
					"(선언=%q 관측=%q)", devID, eff, why, s.class, s.obs)
			}
		}
		if s.class == "" {
			if !w.classGaveUp[devID] { // 유예를 이미 소진했으면 다시 붙들지 않는다
				pendingClass++
				w.classWait[devID]++
			}
		} else {
			delete(w.classWait, devID)
		}
	}
	if pendingClass > 0 {
		// 상한을 넘긴 것이 있으면 더 기다리지 않고 진행하되 경고를 남긴다(T-5 규칙).
		overdue := false
		for devID, n := range w.classWait {
			if n > classGraceTicks {
				overdue = true
				log.Printf("[bless-feeder][경고] class 미도착 %s (%d틱=%.0fs 초과) — "+
					"빈 class 로 쌍 판정을 진행한다. 이종 쌍이면 victim 이 해제되지 않을 수 있다.",
					devID, n, float64(n)*w.interval.Seconds())
				delete(w.classWait, devID)
				w.classGaveUp[devID] = true
			}
		}
		if !overdue {
			// ★보류하는 것은 **relaxed 해제 판정**뿐이다. 신규 테넌트를 arm 하지 않고
			//   보류하면 게이트가 아예 걸리지 않아 시간 축이 죽는다(1차 수정의 회귀).
			//   안전 기본값은 strict(arm) — relaxed 는 이종 쌍 근거가 있을 때만 내린다.
			for devID := range live {
				if _, decided := w.armed[devID]; decided {
					continue
				}
				if err := w.ctrl.FeederArm(devID); err != nil {
					log.Printf("[bless-feeder] 기본 arm 실패 %s: %v", devID, err)
					continue
				}
				w.armed[devID] = true
				log.Printf("[bless-feeder] 기본 arm(class 대기중): %s", devID)
			}
			log.Printf("[bless-feeder] class 미도착 %d건 — relaxed 판정만 보류(다음 폴링 대기)", pendingClass)
			w.reconcileVanished(live)
			return
		}
	}
	want := relaxedDecision(classes)
	for devID, arm := range want {
		if cur, ok := w.armed[devID]; ok && cur == arm {
			continue // 상태 동일 — 재호출 생략
		}
		// [Exp_138 2-C] DWELL — 전환 직후 재전환을 막는다. ★첫 배치는 막지 않는다
		//   (아직 arm 상태가 없으면 게이트가 아예 안 걸린다 = 시간 축 사망).
		if _, decided := w.armed[devID]; decided && scannerLink {
			if last, ok := w.lastSwitch[devID]; ok && w.tick-last < obsDwellTicks {
				continue
			}
		}
		var err error
		if arm {
			err = w.ctrl.FeederArm(devID)
		} else {
			err = w.ctrl.FeederRelease(devID) // victim 해제(ungate, Σt>1 relaxed)
		}
		if err != nil {
			log.Printf("[bless-feeder] arm/release 실패 %s: %v", devID, err)
			continue
		}
		_, wasDecided := w.armed[devID]
		w.armed[devID] = arm
		w.lastSwitch[devID] = w.tick
		mode := map[bool]string{true: "strict", false: "relaxed"}[arm]
		log.Printf("[bless-feeder] 쌍판정: %s → %s (class=%q 사유=%s)", devID,
			map[bool]string{true: "arm(gate)", false: "release(victim)"}[arm],
			classes[devID], reasons[devID])
		// [Exp_138 2-B 5] 전환마다 사유를 실은 이벤트. ★③ 에 이벤트를 처음 붙이는 것이다
		//   (0-A: 지금까지 ControlModeSwitched 는 ④ 의 단계 전환에만 있었다).
		//   첫 배치는 전환이 아니므로 발행하지 않는다.
		if wasDecided && w.ctrl != nil {
			if err := w.ctrl.ModeEvent(devID, mode, reasons[devID],
				classes[devID]); err != nil {
				// [T-5] 조용한 실패 금지 — 제어는 계속하되 사실을 남긴다
				log.Printf("[bless-feeder][경고] 모드 이벤트 발행 실패 %s: %v "+
					"(제어는 적용됨, 기록만 누락)", devID, err)
			}
		}
	}
	// 3) 사라진 소켓 해제 (파드 종료 = stale 방지)
	w.reconcileVanished(live)
}

// reconcileVanished deregisters sockets that disappeared. [Exp_107 T-2] 쌍 판정을
// 보류하는 경로에서도 stale 정리는 계속되어야 하므로 분리했다.
func (w *blessFeederWirer) reconcileVanished(live map[string]scanned) {
	for devID := range w.registered {
		if _, ok := live[devID]; !ok {
			if err := w.ctrl.FeederDeregister(devID); err != nil {
				log.Printf("[bless-feeder] deregister 실패 %s: %v", devID, err)
			}
			delete(w.registered, devID)
			delete(w.armed, devID)
			delete(w.classWait, devID)
			delete(w.classGaveUp, devID) // devID 재사용 대비 초기화
			delete(w.registeredSock, devID)
			// [Exp_138 2-D / T-7] 관측 상태도 같은 자리에서 지운다.
			//   ★Exp_126 §5-4 에서 선언 class·podref 를 지우지 않아 devID 재사용 시
			//     앞 파드의 값이 남아 사고가 났다. 같은 실수를 옮기지 않는다.
			delete(w.obsSeen, devID)
			delete(w.obsHold, devID)
			delete(w.lastSwitch, devID)
			delete(w.effReason, devID)
			// 디스크의 관측 산출물도 지운다 — 다음 파드가 앞 파드의 분류를 물려받지
			// 않게 한다(파일이 남으면 게이트 통과 전인데 통과한 것처럼 보인다).
			for _, f := range []string{"obs_class", "obs_meta.json"} {
				_ = os.Remove(filepath.Join(w.sockDir, devID, f))
			}
			log.Printf("[bless-feeder] 자동 해제(소켓 소멸): %s", devID)
		}
	}
}

func (w *blessFeederWirer) Start() {
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		t := time.NewTicker(w.interval)
		defer t.Stop()
		for {
			select {
			case <-w.stopCh:
				return
			case <-t.C:
				w.reconcile()
			}
		}
	}()
	log.Printf("[bless-feeder] GPU 시간축 자동 배선 시작 (scan %s every %s)",
		w.sockDir, w.interval)
}

func (w *blessFeederWirer) Stop() {
	close(w.stopCh)
	w.wg.Wait()
}
