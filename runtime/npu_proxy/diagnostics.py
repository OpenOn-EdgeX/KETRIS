"""Opt-in experiment observer. Production GateCore and its defaults stay intact.

The nominal denominator is a single-variable counterfactual, NOT a production fix:
it can reintroduce idle-tenant grant accumulation and requires separate evaluation.
"""
import threading
import time
import hashlib
from collections import defaultdict
from gate_core import GateCore


def window_drained(stats):
    return bool(stats) and all(t.get('queue') == 0 and t.get('diag_inflight') == 0 for t in stats.values())


def summarize_window(before, after):
    keys = ('diag_requested_us', 'diag_clipped_us', 'granted_us', 'charged_us', 'charged_extra_us', 'done', 'done_extra')
    deltas = {name: {k: after[name][k]-before[name][k] for k in keys} for name in after}
    def share(name, key):
        total = sum(row[key] for row in deltas.values())
        return deltas[name][key]/total if total > 0 else None
    for row in deltas.values():
        row['accepted_us'] = row['diag_requested_us']-row['diag_clipped_us']
        row['total_accounted_us'] = row['charged_us']+row['charged_extra_us']
    return {name: dict(requested_share=share(name, 'diag_requested_us'), accepted_share=share(name, 'accepted_us'),
                       denominator_share=share(name, 'granted_us'), contract_share=share(name, 'charged_us'),
                       total_accounted_share=share(name, 'total_accounted_us'), completion_share=share(name, 'done'),
                       clipped_us=row['diag_clipped_us'], deltas=row) for name, row in deltas.items()}


class DiagnosticGate(GateCore):
    def __init__(self, run_fn, clock=time.monotonic, work_conserving=True, denominator='accepted'):
        if denominator not in ('accepted', 'nominal'):
            raise ValueError('unknown denominator control')
        super().__init__(run_fn, clock=clock, work_conserving=work_conserving)
        # Preserve mutual exclusion and atomically observe around the base command.
        self._lock = threading.RLock()
        self.denominator = denominator
        self._requested = defaultdict(float)
        self._clipped = defaultdict(float)
        self._inflight = defaultdict(int)
        self._events = []
        self._local = threading.local()

    def cmd(self, name, cmd, arg=None):
        with self._lock:
            t = self._tenants.get(name)
            before = t.credit_us if t else None
            granted = t.granted_us if t else None
            result = super().cmd(name, cmd, arg)
            if t is not None and cmd == 'time_add':
                requested = float(arg)
                accepted = t.credit_us-before
                self._requested[name] += requested
                self._clipped[name] += requested-accepted
                if self.denominator == 'nominal':
                    t.granted_us = granted+requested
                self._events.append(dict(kind='grant', at=self._clock(), tenant=name,
                    requested_us=requested, accepted_us=accepted, clipped_us=requested-accepted,
                    credit_before_us=before, credit_after_us=t.credit_us, granted_us=t.granted_us))
            return result

    def _eligible(self):
        tenant, extra = super()._eligible()
        self._local.selected = tenant.name if tenant else None
        if tenant:
            self._inflight[tenant.name] += 1
            self._events.append(dict(kind='selected', at=self._clock(), tenant=tenant.name, extra=extra,
                states={n:dict(credit_us=t.credit_us, granted_us=t.granted_us, charged_us=t.charged_us,
                               queued=len(t.queue), inflight=self._inflight[n]) for n, t in self._tenants.items()}))
        return tenant, extra

    def step(self):
        self._local.selected = None
        try:
            result = super().step()
            with self._lock:
                if result:
                    text = getattr(result[1], 'result', None)
                    self._events.append(dict(kind='completed', at=self._clock(), tenant=result[0], elapsed_us=result[2],
                        generated_text_chars=len(text) if isinstance(text, str) else None,
                        generated_text_sha256=hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None))
            return result
        finally:
            name = self._local.selected
            if name:
                with self._lock:
                    self._inflight[name] -= 1

    def stats(self):
        with self._lock:
            values = super().stats()
            for name, row in values.items():
                row.update(diag_requested_us=self._requested[name], diag_clipped_us=self._clipped[name],
                           diag_inflight=self._inflight[name])
            return values

    def drain_events(self):
        with self._lock:
            events, self._events = self._events, []
            return events
