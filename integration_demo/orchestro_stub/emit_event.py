#!/usr/bin/env python3
# 이 컴포넌트는 오케스트로 기관 구현으로 대체됩니다. A-4 K8s Event 검증용 최소 스텁입니다.
# 최소 범위: A-4 규격의 reason/involvedObject/message로 Kubernetes Event 하나 발행.
# usage: emit_event.py <reason> <kind> <name> <namespace> <message>
import subprocess, sys, time, json
from datetime import datetime, timezone

reason, kind, name, ns, msg = sys.argv[1:6]
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # [Exp_82] timestamp 채움
# resourceVersion 필요없는 최소 Event. eventTime은 고정 문자열(런타임 시각은 호출측 로그에).
ev = {
    "apiVersion": "v1", "kind": "Event",
    "metadata": {"generateName": "kraken-demo-", "namespace": ns},
    "type": "Normal" if reason not in ("InterferenceDetected", "ControlAxisFailed") else "Warning",
    "reason": reason, "message": msg,
    "involvedObject": {"kind": kind, "name": name, "namespace": ns},
    "source": {"component": "kraken-orchestro-stub"},
    "reportingComponent": "kraken-orchestro-stub", "reportingInstance": "demo",
    "action": reason, "firstTimestamp": now, "lastTimestamp": now, "eventTime": now,
}
subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(ev).encode(),
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"[event] {reason} {kind}/{name}: {msg}")
