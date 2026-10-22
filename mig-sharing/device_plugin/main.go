package main

import (
	"encoding/json"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	path := flag.String("config", "", "partition config JSON")
	dir := flag.String("socket-dir", "/var/lib/kubelet/device-plugins", "kubelet plugin directory")
	flag.Parse()
	raw, err := os.ReadFile(*path)
	if err != nil {
		log.Fatal(err)
	}
	var cfg Config
	if err = json.Unmarshal(raw, &cfg); err != nil {
		log.Fatal(err)
	}
	p, err := NewPlugin(cfg, *path)
	if err != nil {
		log.Fatal(err)
	}
	if err = p.Start(*dir); err != nil {
		log.Fatal(err)
	}
	log.Printf("registered MIG share resource=%s UUID=%s logical_units=%d", cfg.ResourceName, cfg.MIGUUID, cfg.Units)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	p.Stop()
}
