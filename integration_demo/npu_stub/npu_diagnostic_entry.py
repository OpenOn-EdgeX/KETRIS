"""Run an exact copied serve with optional observer; bind only host loopback."""
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, '/gate')
import gate_core
from diagnostics import DiagnosticGate

out = Path('/out')
instances = []
stop = threading.Event()


class ObservedGate(DiagnosticGate):
    def __init__(self, run_fn, **kwargs):
        super().__init__(run_fn, denominator=os.environ.get('DIAG_DENOMINATOR', 'accepted'), **kwargs)
        instances.append(self)


gate_core.GateCore = ObservedGate


def flush():
    with (out / 'events.jsonl').open('a') as f:
        for gate in instances:
            for event in gate.drain_events():
                f.write(json.dumps(event) + '\n')


def sampler():
    while not stop.wait(.2):
        flush()
        for gate in instances:
            with (out / 'samples.jsonl').open('a') as f:
                f.write(json.dumps(dict(monotonic=time.monotonic(), wall=time.time(), tenants=gate.stats())) + '\n')


thread = threading.Thread(target=sampler, daemon=True)
thread.start()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    source = Path('/work/npu_gate_serve.py').read_text()
    old = 'ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()'
    if source.count(old) != 1:
        raise RuntimeError('serve source changed; refusing an unknown host bind')
    source = source.replace(old, 'ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()')
    exec(compile(source, '/work/npu_gate_serve.py', 'exec'), dict(__name__='__main__', __file__='/work/npu_gate_serve.py'))
finally:
    stop.set()
    thread.join(2)
    flush()
