package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// [Exp_127 4부] 선기록 정리 — 안전장치 두 개가 실제로 작동하는지.
func TestSweepPrewrites(t *testing.T) {
	dir := t.TempDir()
	mk := func(dev, f string, age time.Duration) string {
		d := filepath.Join(dir, dev)
		os.MkdirAll(d, 0o777)
		p := filepath.Join(d, f)
		os.WriteFile(p, []byte("x\n"), 0o666)
		old := time.Now().Add(-age)
		os.Chtimes(p, old, old)
		return p
	}
	stale := mk("dev-stale", "class", 10*time.Minute)
	staleRef := mk("dev-stale", "podref", 10*time.Minute)
	fresh := mk("dev-fresh", "class", 1*time.Second)      // 유예 안 — 남아야 함
	inuse := mk("dev-inuse", "class", 10*time.Minute)     // 점유 중 — 남아야 함
	sock := mk("dev-stale", "a.sock", 10*time.Minute)     // 소켓은 건드리지 않는다

	w := &PodWatcher{blessSockDir: dir}
	w.sweepPrewrites(map[string]bool{"dev-inuse": true})

	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("낡은 class 는 지워져야 한다")
	}
	if _, err := os.Stat(staleRef); !os.IsNotExist(err) {
		t.Fatal("낡은 podref 는 지워져야 한다")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatal("유예 안의 선기록은 남아야 한다 — 갓 할당된 파드의 class 를 지우면 짝짓기가 무너진다")
	}
	if _, err := os.Stat(inuse); err != nil {
		t.Fatal("점유 중인 devID 의 선기록은 남아야 한다")
	}
	if _, err := os.Stat(sock); err != nil {
		t.Fatal("소켓 파일은 건드리지 않는다 — feeder sockAlive() 담당")
	}
}

// blessSockDir 미설정이면 아무 것도 하지 않는다.
func TestSweepNoDir(t *testing.T) {
	w := &PodWatcher{}
	w.sweepPrewrites(map[string]bool{}) // panic 없이 반환하면 통과
}
