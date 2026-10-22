package main

import (
	"errors"
	"flag"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
)

// buildStamp 는 빌드 시 -ldflags 로 주입한다(tools/build_stamp.sh).
//
//	기본값이 그대로 뜨면 스탬프 없이 빌드된 것 — deploy_verify 가 실패시킨다.
var buildStamp = "unstamped"

func main() {
	controllerURL := flag.String("controller-url", "http://localhost:8090",
		"KRAKEN 컨트롤러 HTTP URL")
	slicesPerGPU := flag.Int("slices-per-gpu", 4,
		"물리 GPU 당 논리 슬라이스 수 (slice 모드)")
	resourceName := flag.String("resource-name", "",
		"K8s Extended Resource 이름 (비우면 모드별 기본값: lsu→keti.re.kr/lsu, slice→keti.re.kr/gpu-slice)")
	lsuMode := flag.Bool("lsu", false,
		"LSU 모드: GPU를 catalog 기반 LSU 수량으로 노출 (resource keti.re.kr/lsu)")
	catalogPath := flag.String("catalog", "../lsu/catalog.json",
		"LSU 카탈로그 경로 (lsu 모드)")
	dryRun := flag.Bool("dry-run", false,
		"광고 내용·Allocate 매핑만 로그로 출력하고 종료 (kubelet 등록·controller 변경 없음)")
	sampleLSU := flag.Int("sample-lsu", 13,
		"dry-run Allocate 시뮬레이션에서 요청할 LSU 수")
	kubeconfig := flag.String("kubeconfig", "",
		"kubeconfig 경로 (비어있으면 in-cluster ServiceAccount 사용)")
	npuMode := flag.Bool("npu", false,
		"NPU 모듈 활성화: Warboy PE 단위로 keti.io/npu 광고 (Exp_33)")
	rngdMode := flag.Bool("rngd", false,
		"RNGD 모듈 활성화: PE 단위로 keti.re.kr/npu-pe 광고 (Exp_70)")
	rngdDevRoot := flag.String("rngd-dev-root", "/dev/rngd",
		"RNGD 디바이스 노드 루트 (테스트용)")
	npuDevRoot := flag.String("npu-dev-root", "/dev",
		"NPU 디바이스 노드 루트 (테스트용)")
	npuProxyModel := flag.String("npu-proxy-model", "",
		"프록시 서비스 모델명 (예: resnet50) — 지정 시 프록시 PE 를 직접 풀에서 "+
			"제외하고 keti.io/npu-<model> 슬롯으로 광고 (Exp_47 게이트 1B/2A)")
	npuProxyPE := flag.String("npu-proxy-pe", "npu0pe0",
		"프록시 전용 PE (직접 할당 풀에서 제외)")
	npuProxyDir := flag.String("npu-proxy-dir", "/var/lib/kraken/npu",
		"프록시 소켓 hostPath 디렉토리 (모델 리소스 파드에 마운트)")
	npuRedistribute := flag.Bool("npu-redistribute", false,
		"모델 슬롯 테넌트 이탈 시 남은 테넌트 비례 재정규화 (정책 B, Exp_53 — 기본 A)")
	var npuProxies npuProxyList
	flag.Var(&npuProxies, "npu-proxy",
		"추가 프록시 PE (반복 가능): model=<이름>,pe=<npuXpeY>[,dir=<hostPath>] — "+
			"Exp_59 다중 모델. --npu-proxy-model 과 함께 쓰면 그것이 첫 모델. "+
			"각 모델은 서로 다른 PE 여야 한다(물리 분리 = 이중계상 없음).")
	blessSO := flag.String("bless-so", "",
		"libbless.so 호스트 경로 — 지정 시 GPU Allocate 가 LD_PRELOAD 주입 (Exp_33)")
	mpsPipeDir := flag.String("mps-pipe-dir", "",
		"MPS 파이프 디렉토리 (예: /tmp/nvidia-mps) — 지정 시 GPU Allocate 가 "+
			"마운트 + BLESS_SPACE_MODE=mps_env 주입 (Exp_71 Part B)")
	blessSockDir := flag.String("bless-sock-dir", "",
		"호스트 디렉토리 — 컨테이너 /tmp 로 마운트해 bless 제어 소켓을 호스트에 노출")
	tracerSO := flag.String("tracer-so", "",
		"kernel_tracer.so 호스트 경로 — --cupti-dir 과 함께 지정 시 GPU Allocate 가 "+
			"CUPTI 커널 관측을 LD_PRELOAD 주입 (Exp_131). 기본 꺼짐")
	cuptiDir := flag.String("cupti-dir", "",
		"libcupti.so.12 가 있는 호스트 디렉토리 — --tracer-so 와 짝 (Exp_131)")
	tracerFlushMS := flag.Int("tracer-flush-ms", 100,
		"KTRACE_FLUSH_MS — 커널 레코드 플러시 주기 (Exp_19 검증 구성)")
	tracerBufKB := flag.Int("tracer-buf-kb", 64,
		"KTRACE_BUF_KB — CUPTI 활동 버퍼 크기 (Exp_19 검증 구성)")
	// [Exp_144 4부] 기본 켜짐으로 전환. 경로 기본값 = entrypoint 가 place() 로
	//   반드시 놓는 자리(entrypoint-plugin.sh:28-29). 켜고 끄기는 아래
	//   KRAKEN_FAST_TEARDOWN 스위치가 맡는다(코드 기본=DS env="1" 일치).
	fastTeardownDir := flag.String("fast-teardown-dir", "/var/lib/kraken/py",
		"sitecustomize.py 가 든 호스트 디렉터리 — GPU Allocate 가 "+
			"PYTHONPATH + BLESS_FAST_TEARDOWN=1 을 주입 (Exp_134·144). "+
			"KRAKEN_FAST_TEARDOWN=0 이면 주입하지 않는다")
	flag.Parse()

	// 모드별 기본 resource 이름 (기존 slice 동작 보존).
	if *resourceName == "" {
		if *lsuMode {
			*resourceName = "keti.re.kr/lsu"
		} else {
			*resourceName = "keti.re.kr/gpu-slice"
		}
	}

	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	// [Exp_123 2부] 배포 검증 — 도는 바이너리가 의도한 소스인지 대조할 근거.
	//   태그만으로는 "같은 태그·다른 내용"(Exp_121)을 못 잡는다.
	log.Printf("[main] build=%s", buildStamp)
	// [Exp_134] 스탬프를 파일로도 남긴다 — kubelet 로그는 회전한다.
	//   Exp_134 0부에서 실제로 발생: 파드 기동 2.5시간 뒤 build= 줄이 로그에서
	//   밀려나 deploy_verify 가 **정상 배포를 실패로 판정**했다. 로그가 사라져도
	//   실행체가 남긴 근거가 있어야 한다. 파일 mtime 을 파드 기동 시각과 대조하면
	//   구 파드가 남긴 잔재로 오판할 여지도 없다(deploy_verify 가 확인).
	if err := os.WriteFile("/var/lib/kraken/stamp-plugin",
		[]byte(buildStamp+"\n"), 0o644); err != nil {
		log.Printf("[main][경고] 스탬프 파일 기록 실패: %v — "+
			"deploy_verify 는 로그 경로로만 검증하게 된다", err)
	}
	log.Printf("[main] KRAKEN Device Plugin 시작 (mode=%s dry-run=%v)",
		map[bool]string{true: "LSU", false: "slice"}[*lsuMode], *dryRun)
	log.Printf("[main]   resource    = %s", *resourceName)
	log.Printf("[main]   controller  = %s", *controllerURL)

	// ── 컨트롤러 접속 확인 ────────────────────────────────────────────────────
	// dry-run에서는 controller 미가동이어도 SM 0으로 진행(노출 로직만 검증).
	ctrl := NewControllerClient(*controllerURL)
	status, err := ctrl.GetStatus()
	if err != nil {
		if !*dryRun {
			log.Fatalf("[main] controller 접속 실패: %v", err)
		}
		log.Printf("[main] controller 접속 실패(dry-run 계속): %v", err)
		status = &ControllerStatus{GroupID: "default", PhysicalSM: 0, PhysicalMemMB: 0, Mode: "N/A"}
	} else {
		log.Printf("[main] controller 연결 완료: group=%s SM=%d mem=%dMB mode=%s",
			status.GroupID, status.PhysicalSM, status.PhysicalMemMB, status.Mode)
	}

	// ── GPU 감지 ──────────────────────────────────────────────────────────────
	gpus, err := DiscoverGPUs()
	if err != nil {
		log.Fatalf("[main] GPU 감지 실패: %v", err)
	}
	// SM count는 controller /status에서 가져옴.
	// 현재 kraken_controller는 단일 GPU 그룹을 관리하므로
	// physical_sm_total을 모든 GPU에 균등 배분.
	var smPerGPU int32
	if len(gpus) > 0 {
		smPerGPU = status.PhysicalSM / int32(len(gpus))
	}
	if smPerGPU == 0 {
		smPerGPU = status.PhysicalSM
	}

	// ── LSU 모드: catalog 로드 후 GPU별 LSU 산정 ──────────────────────────────
	var catalog *LSUCatalog
	if *lsuMode {
		catalog, err = LoadCatalog(*catalogPath)
		if err != nil {
			log.Fatalf("[main] LSU catalog 로드 실패: %v", err)
		}
		log.Printf("[main] LSU catalog: workload=%s unit=%.2f img/s/LSU (%s)",
			catalog.ReferenceWorkload, catalog.UnitImgPerS, catalog.Status)
	}

	for i := range gpus {
		gpus[i].SmCount = smPerGPU
		if *lsuMode {
			lsu, src := catalog.LSUFor(gpus[i].Name)
			gpus[i].LSU = lsu
			gpus[i].LSUSource = src
			log.Printf("[main]   GPU%d: %s  mem=%dMB  sm=%d  LSU=%d (%s)",
				gpus[i].Index, gpus[i].Name, gpus[i].MemoryMB, gpus[i].SmCount, lsu, src)
		} else {
			log.Printf("[main]   GPU%d: %s  mem=%dMB  sm=%d",
				gpus[i].Index, gpus[i].Name, gpus[i].MemoryMB, gpus[i].SmCount)
		}
	}

	// ── Device Plugin 생성 ────────────────────────────────────────────────────
	plugin := NewDevicePlugin(*resourceName, *slicesPerGPU, *lsuMode, ctrl, gpus)
	if *lsuMode && catalog != nil && catalog.OvercommitFactor > 0 {
		plugin.SetOvercommitFactor(catalog.OvercommitFactor) // 광고 device 맵 재생성
		log.Printf("[main] 오버커밋 factor=%.2f (물리 LSU→광고 LSU ×factor; "+
			"컴퓨트 pct÷물리, 메모리 quota÷광고 — Σpct>100%% 정상)", plugin.ocFactor)
	}
	plugin.blessSO = *blessSO
	plugin.blessSockDir = *blessSockDir
	plugin.mpsPipeDir = *mpsPipeDir
	if *mpsPipeDir != "" {
		log.Printf("[main] MPS 모드 활성: pipe-dir=%s (BLESS_SPACE_MODE=mps_env 주입)",
			*mpsPipeDir)
	}
	if *blessSO != "" {
		// [Exp_131 T-5/T-7] 주입 대상이 호스트에 없으면 **조용히 넘어가지 않는다.**
		//   파일이 없으면 kubelet 이 hostPath 를 빈 디렉토리로 만들어 주므로
		//   LD_PRELOAD 는 실패하고 워크로드는 그냥 제어 없이 돈다 — 지금까지
		//   이 경로에 검증이 없었다(Exp_131 1-B 에서 확인). 기동 시 거부한다.
		if st, err := os.Stat(*blessSO); err != nil || st.IsDir() {
			log.Fatalf("[main] --bless-so=%s 를 열 수 없다(err=%v) — "+
				"주입이 조용히 무력화되므로 기동을 거부한다", *blessSO, err)
		}
		log.Printf("[main] libbless 주입 활성: so=%s sock-dir=%s",
			*blessSO, *blessSockDir)
	}

	// [Exp_131] CUPTI 커널 관측 주입 준비물 — 경로는 짝으로만 유효하다.
	if (*tracerSO == "") != (*cuptiDir == "") {
		log.Fatalf("[main] --tracer-so 와 --cupti-dir 은 함께 지정해야 한다 "+
			"(tracer-so=%q cupti-dir=%q) — 한쪽만으로는 tracer 가 libcupti 를 "+
			"찾지 못해 워크로드가 기동하지 못한다", *tracerSO, *cuptiDir)
	}
	// [Exp_140 1-C] 주입 스위치 — KRAKEN_TRACER=1 일 때만 주입한다. 기본 꺼짐.
	//   Exp_139 확인: 플래그만 있으면 전 GPU Allocate 에 무조건 주입됐다(기록량
	//   파드당 최대 9.2MB/s — 상시 적재를 막아온 근거와 충돌). 스위치 형태는
	//   KRAKEN_REDIST/BOOSTER/CTLCHECK 패턴을 따르고, 코드 기본값과 DS env 를
	//   "0"으로 일치시킨다(KRAKEN_CTLCHECK 처럼 어긋나는 상태를 만들지 않는다).
	//   파드 단위 스위치는 불가 — device plugin Allocate 요청에는 device ID 만
	//   오고 파드 정체가 없다(kubelet API 한계). 노드 단위로 둔다.
	tracerOn := os.Getenv("KRAKEN_TRACER") == "1"
	if tracerOn && *tracerSO == "" {
		log.Fatalf("[main] KRAKEN_TRACER=1 인데 --tracer-so/--cupti-dir 미지정 — " +
			"주입할 파일 경로가 없다. 조용히 무시하지 않고 기동을 거부한다 (T-5)")
	}
	if *tracerSO != "" && !tracerOn {
		log.Printf("[main] CUPTI tracer 준비됨(경로 지정) — 주입은 꺼짐 " +
			"(KRAKEN_TRACER != 1). 켜려면 DS env KRAKEN_TRACER=1")
	}
	if *tracerSO != "" && tracerOn {
		if st, err := os.Stat(*tracerSO); err != nil || st.IsDir() {
			log.Fatalf("[main] --tracer-so=%s 를 열 수 없다(err=%v) — "+
				"LD_PRELOAD 실패로 **워크로드 파드가 기동하지 못한다**. 거부한다",
				*tracerSO, err)
		}
		cuptiSO := filepath.Join(*cuptiDir, "libcupti.so.12")
		if st, err := os.Stat(cuptiSO); err != nil || st.IsDir() {
			log.Fatalf("[main] %s 를 열 수 없다(err=%v) — 거부한다", cuptiSO, err)
		}
		if *blessSockDir == "" {
			log.Fatalf("[main] CUPTI 주입은 --bless-sock-dir 이 필요하다 — " +
				"KTRACE_OUT(/tmp/trace.txt)이 호스트로 나올 자리가 없다")
		}
		plugin.tracerSO = *tracerSO
		plugin.cuptiDir = *cuptiDir
		plugin.tracerFlushMS = *tracerFlushMS
		plugin.tracerBufKB = *tracerBufKB
		log.Printf("[main] CUPTI 커널 관측 주입 활성: tracer=%s cupti=%s "+
			"flush=%dms buf=%dKB", *tracerSO, *cuptiDir, *tracerFlushMS, *tracerBufKB)
	}

	// [Exp_134 3-B] 종료 감지 자동 등록 — controller 와 **같은 스위치**를 쓴다.
	//   플래그를 따로 두면 둘이 어긋난 채로 돌 수 있다(한쪽만 켜지면 등록은
	//   되는데 소비자가 없거나, 소비자는 있는데 등록이 없다).
	boosterOn := os.Getenv("KRAKEN_BOOSTER") == "1"
	if boosterOn {
		log.Printf("[main] 종료 감지 자동 등록 활성 (KRAKEN_BOOSTER=1) — " +
			"Allocate 가 Booster 에 등록하고 파드 종료 시 해제한다")
	}
	plugin.boosterRegister = boosterOn

	// [Exp_134] 빠른 종료 주입 — 기본 꺼짐. T-7: libbless/tracer 경로와 같은
	//   기동 시 존재 검증을 붙인다. 여기서 거부하지 않으면 PYTHONPATH 만 걸리고
	//   sitecustomize 는 없는 상태가 되어 **워크로드가 아무 일 없이 그냥 돈다** —
	//   Exp_131 이 --bless-so 에서 찾아낸 것과 정확히 같은 조용한 무력화다.
	// [Exp_144 4부] 기본 켜짐 — 끄려면 KRAKEN_FAST_TEARDOWN=0.
	//   근거: 파일 유실 기전 해소(레지스트리 flush, 실측 0.1ms·바이트 일치) +
	//   해지 22.95% 단축(n=100, 목표 10% 대비 2.3배). flush 유무 차이는
	//   +9.1ms(0.47%)로 잡음과 구분되지 않는다(Mann-Whitney p=0.61).
	ftOn := os.Getenv("KRAKEN_FAST_TEARDOWN") != "0"
	if ftOn && *fastTeardownDir != "" {
		sc := filepath.Join(*fastTeardownDir, "sitecustomize.py")
		if st, err := os.Stat(sc); err != nil || st.IsDir() {
			log.Fatalf("[main] %s 를 열 수 없다(err=%v) — PYTHONPATH 만 걸리고 "+
				"빠른 종료는 무동작이 된다. 기동을 거부한다", sc, err)
		}
		plugin.fastTeardownDir = *fastTeardownDir
		log.Printf("[main] 빠른 종료 주입 활성(기본 켜짐, Exp_144): dir=%s "+
			"(PYTHONPATH + BLESS_FAST_TEARDOWN=1). ⚠️ 파이썬이 아닌 워크로드에서는 "+
			"무동작이며 플러그인은 그것을 알 수 없다", *fastTeardownDir)
	} else {
		log.Printf("[main] 빠른 종료 주입 꺼짐 (KRAKEN_FAST_TEARDOWN=%q dir=%q)",
			os.Getenv("KRAKEN_FAST_TEARDOWN"), *fastTeardownDir)
	}

	// ── dry-run: 노출·Allocate 매핑만 출력하고 종료 ───────────────────────────
	if *dryRun {
		plugin.DryRun(*sampleLSU)
		return
	}

	if err := plugin.Start(); err != nil {
		log.Fatalf("[main] Device Plugin 시작 실패: %v", err)
	}

	// ── NPU 모듈 (Exp_33 — keti.io/npu PE 단위; Exp_47 — 프록시 PE 풀 분할;
	//    Exp_59 — 다중 모델: 프록시 PE 를 여러 개 두고 각각 모델 리소스로 광고) ──
	var npu *NPUPlugin
	var npuModels []*NPUModelPlugin
	if *npuMode {
		proxies, perr := resolveNPUProxies(*npuProxyModel, *npuProxyPE,
			*npuProxyDir, npuProxies)
		if perr != nil {
			log.Fatalf("[main] NPU 프록시 구성 오류: %v", perr)
		}
		excludePEs := make([]string, 0, len(proxies))
		for _, pr := range proxies {
			excludePEs = append(excludePEs, pr.PE) // 게이트 1(B): 이중계상 원천 차단
		}
		npu, err = NewNPUPlugin(*npuDevRoot, excludePEs...)
		if errors.Is(err, ErrNoDirectPEs) {
			// 다중 모델에서 전 PE 가 프록시 소유 = 정상 구성. 직접 경로만 없다
			// (Exp_59 트레이드오프 — 모델 수를 늘린 대가로 keti.io/npu 상실).
			log.Printf("[main] keti.io/npu 광고 생략: 직접 할당 PE 0개 " +
				"(전 PE 가 프록시 전용 — 다중 모델 구성의 트레이드오프)")
			npu = nil
		} else if err != nil {
			log.Printf("[main] NPU 모듈 비활성 (감지 실패): %v", err)
			npu = nil
		} else if err := npu.Start(); err != nil {
			log.Fatalf("[main] NPU Plugin 시작 실패: %v", err)
		}
		for i, pr := range proxies {
			m := NewNPUModelPluginN(pr.Model, pr.PE, pr.Dir, i)
			m.controller = ctrl // Exp_53: feeder 자동 등록
			m.redistribute = *npuRedistribute
			if err := m.Start(); err != nil {
				log.Fatalf("[main] NPU 모델 리소스 시작 실패 (%s): %v", pr.Model, err)
			}
			npuModels = append(npuModels, m)
		}
	}

	// ── RNGD 모듈 (Exp_70 — keti.re.kr/npu-pe PE 단위, 정렬 블록 강제) ────────
	var rngd *RNGDPlugin
	if *rngdMode {
		rngd, err = NewRNGDPlugin(*rngdDevRoot)
		if err != nil {
			log.Printf("[main] RNGD 모듈 비활성 (감지 실패): %v", err)
			rngd = nil
		} else if err := rngd.Start(); err != nil {
			log.Fatalf("[main] RNGD Plugin 시작 실패: %v", err)
		}
	}

	// ── Pod Watcher 시작 (deregister 담당) ───────────────────────────────────
	watcher, err := NewPodWatcher(*resourceName, ctrl, *kubeconfig)
	if err != nil {
		log.Printf("[main] Pod Watcher 초기화 실패 (deregister 비활성): %v", err)
	} else {
		// Exp_41: 등록에 실제 사용된 ID 로만 deregister (watcher 위생)
		watcher.SetRegistryLookup(plugin.IsRegisteredDevice, plugin.ForgetDevice)
		watcher.SetAllocatedLister(plugin.AllocatedOlderThan) // [Exp_97 Part D]
		// [Exp_110 R-1] class 선기록 — libbless init 을 기다리지 않고 파드 env 에서 즉시 쓴다.
		watcher.SetBlessSockDir(*blessSockDir)
		watcher.booster = boosterOn // [Exp_134] 종료 시 Booster 해제
		// Exp_53: NPU 모델 슬롯 소멸 자동 정리 + DS 재기동 복원(라이브 파드 필터
		// — stale checkpoint 엔트리의 고아 재등록 차단)
		for _, m := range npuModels {
			watcher.SetNPUWatch(m.resourceName, m.CleanupTenant)
			m.RestoreFromCheckpoint(kubeletCheckpointPath, watcher.LivePodUIDs())
		}
		watcher.Start()
		log.Printf("[main] Pod Watcher 시작: checkpoint=%s", kubeletCheckpointPath)
	}

	// ── GPU 시간축 자동 배선 (Exp_73 Part B) ─────────────────────────────────
	// bless 제어 소켓 감지 → feeder register+arm. blessSockDir 이 있어야 소켓이
	// 호스트에 노출된다. lsuMode(GPU libbless 경로)에서만 의미.
	var blessWirer *blessFeederWirer
	if *lsuMode && *blessSockDir != "" && len(gpus) > 0 {
		// [Exp_140 5-A] 재기동 시 할당 복원 — 와이어러 시작 **전에** 해야
		// 첫 스캔부터 라이브 소켓이 stale 로 배제되지 않는다. 이것 없이는
		// 롤아웃마다 기존 armed 파드의 시간축 공급이 끊긴다(plugin.go 주석).
		if n := plugin.RestoreAllocations(kubeletCheckpointPath, *resourceName,
			*blessSockDir); n > 0 {
			log.Printf("[main] 재기동 할당 복원: %d건 (checkpoint → feeder 재배선 대상)", n)
		}
		blessWirer = newBlessFeederWirer(*blessSockDir, ctrl, plugin.RequestedUnits, gpus[0].LSU)
		blessWirer.Start()
	}

	// ── 종료 시그널 대기 ──────────────────────────────────────────────────────
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	<-sig

	log.Println("[main] 종료 신호 수신 — 정리 중...")
	if blessWirer != nil {
		blessWirer.Stop()
	}
	if watcher != nil {
		watcher.Stop()
	}
	if npu != nil {
		npu.Stop()
	}
	if rngd != nil {
		rngd.Stop()
	}
	for _, m := range npuModels {
		m.Stop()
	}
	plugin.Stop()
	log.Println("[main] 종료 완료")
}
