#!/usr/bin/env python3
"""Single-variable real RNGD experiments. No production deployment changes.

Uses an owned containerd task with only PE0-3 and necessary management/DMA nodes.
The original HTTP load generator is reused; the server binds host loopback only.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HISTORY = Path('/root/workspace/prism-server-task/reports/ketris_rngd_repro_20261012')
sys.path.insert(0, str(ROOT / 'runtime/npu_proxy'))
from diagnostics import summarize_window, window_drained


class Experiment:
    def __init__(self, output, seconds, warmup, repeats):
        self.out = Path(output).resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.seconds = seconds; self.warmup = warmup; self.repeats = repeats
        self.process = None; self.task = None; self.log = None
        self.rows = []
        spec = importlib.util.spec_from_file_location('original_rngd_load', HISTORY / 'run.py')
        self.client = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.client)
        self.save('protocol.json', dict(measurement='REAL RNGD inference, local containerd, not K8s Allocate certification',
            model='furiosa-ai/Qwen2.5-0.5B-Instruct', max_tokens=64, prompt='What is the capital of France? Answer in one sentence.',
            devices='npu:0:0-3', gate_workers=2, concurrency_per_tenant=4, tick_seconds=.1, credit_cap_us=1_000_000,
            work_conserving=True, legacy_repay=False, duration=seconds, warmup=warmup, repeats=repeats,
            metrics='generate-call elapsed wall time summed across overlapping workers; not exclusive NPU busy time',
            controls='nominal denominator is diagnostic only, not a deployed fix; idle-return regression remains untested',
            arms=[dict(name='original', budget=300000, denominator='accepted'),
                  dict(name='denominator-only', budget=300000, denominator='nominal'),
                  dict(name='budget-only', budget=200000, denominator='accepted')]))

    def save(self, name, value):
        p = self.out / name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')

    def cmd(self, args, **kw):
        result = subprocess.run(args, capture_output=True, text=True, timeout=kw.pop('timeout', 30), **kw)
        with (self.out / 'commands.log').open('a') as f:
            f.write('$ ' + ' '.join(args) + '\n' + result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('command failed: ' + ' '.join(args) + '\n' + result.stderr)
        return result.stdout

    def npu_idle(self):
        output = self.cmd(['furiosa-smi', 'ps'])
        if re.search(r'^\|\s*\d+\s*\|', output, re.MULTILINE):
            raise RuntimeError('NPU has active external processes; refusing allocation')
        return output

    def prepare(self):
        self.lock = open('/tmp/ketris-npu-time-diagnostic.lock', 'w')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.npu_idle()
        self.save('before.json', dict(npu=self.cmd(['furiosa-smi', 'info']), processes=self.npu_idle(),
            gpu=self.cmd(['nvidia-smi', '-L']), allocatable=self.cmd(['kubectl','get','node','gpu-npu-server-02','-o','jsonpath={.status.allocatable}'])))
        self.save('source-hashes.json', {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT/'runtime/npu_proxy/gate_core.py', ROOT/'runtime/npu_proxy/diagnostics.py',
                      ROOT/'integration_demo/npu_stub/npu_gate_serve.py', ROOT/'integration_demo/npu_stub/npu_diagnostic_entry.py']})
        original = json.loads((HISTORY / 'source-hashes.json').read_text())
        for rel, expected in original.items():
            if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest() != expected:
                raise RuntimeError('original source differs from Oct12 evidence: ' + rel)
        # Freeze source files for every arm; the production source/cache stays untouched.
        for directory in ('work', 'gate'):
            (self.out / directory).mkdir()
        for name in ('gate_core.py', 'diagnostics.py'):
            shutil.copy2(ROOT/'runtime/npu_proxy'/name, self.out/'gate'/name)
        for name in ('npu_gate_serve.py', 'npu_diagnostic_entry.py'):
            shutil.copy2(ROOT/'integration_demo/npu_stub'/name, self.out/'work'/name)
        self.cmd(['cp', '-a', '--reflink=auto', str(HISTORY/'furiosa-cache'), str(self.out/'furiosa-cache')], timeout=120)
        self.save('image.txt.json', dict(reference='docker.io/library/kraken-npu:qwen',
            local_listing=self.cmd(['ctr','-n','k8s.io','images','ls'])))

    def get_stats(self, port):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/stats', timeout=10) as response:
            return json.load(response)

    def stop_task(self):
        if not self.task:
            return
        task = self.task
        # Unique ID is created exclusively by this process; no name-based adoption.
        running = self.cmd(['ctr','-n','k8s.io','tasks','ls','--quiet']).splitlines()
        if task in running:
            self.cmd(['ctr','-n','k8s.io','tasks','kill','--signal','SIGTERM',task])
        if self.process:
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.cmd(['ctr','-n','k8s.io','tasks','kill','--signal','SIGKILL',task])
                self.process.wait(timeout=30)
        if self.log:
            self.log.close(); self.log = None
        self.task = None; self.process = None
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                self.npu_idle(); return
            except RuntimeError:
                time.sleep(.5)
        raise RuntimeError('NPU contexts remain after owned task stopped')

    def drained_stats(self, port):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            stats = self.get_stats(port)
            if window_drained(stats):return stats
            time.sleep(.01)
        raise RuntimeError('request queues/accounting have not drained')

    def condition(self, repeat, name, budget, denominator):
        self.npu_idle()
        label = f'{name}-r{repeat}'
        out = self.out / label; out.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        task = 'ketris-npu-diag-' + hashlib.sha256((str(self.out)+label).encode()).hexdigest()[:16]
        exists = subprocess.run(['ctr','-n','k8s.io','containers','info',task], capture_output=True, text=True, timeout=10)
        if exists.returncode == 0:
            raise RuntimeError('experiment task ID exists; refusing reuse')
        args = ['ctr','-n','k8s.io','run','--rm','--net-host']
        devices = [f'/dev/rngd/npu0{x}' for x in ('mgmt','bar0','bar2','bar4','dmar','p2pmem')]
        devices += [f'/dev/rngd/npu0ch{i}{r}' for i in range(8) for r in ('','r')]
        devices += [f'/dev/rngd/npu0pe{i}' for i in range(4)]
        devices += [f'/dev/rngd/npu0pe{x}' for x in ('0-1','2-3','0-3')]
        for device in devices:
            if not Path(device).exists(): raise RuntimeError('missing RNGD device ' + device)
            args += ['--device', device]
        mounts = [('/root/npu_test/rngd-verify/.venv/lib/python3.10/site-packages','/venv/site-packages','ro'),
                  ('/root/npu_test/rngd-verify/.venv/bin','/venv/bin','ro'),
                  ('/root/.cache/huggingface','/hf','ro'), (str(self.out/'furiosa-cache'),'/furiosa-cache','rw'),
                  (str(self.out/'work'),'/work','ro'), (str(self.out/'gate'),'/gate','ro'), (str(out),'/out','rw')]
        for src,dst,mode in mounts:
            args += ['--mount',f'type=bind,src={src},dst={dst},options=rbind:{mode}']
        env = dict(NPU_MODEL='furiosa-ai/Qwen2.5-0.5B-Instruct',KETI_NPU_DEVICES='npu:0:0-3', GATE_PORT=str(port),
            RATIOS='A:0.7,B:0.3',GATE_BUDGET_US=str(budget),MAX_TOKENS='64',GATE_CREDIT_CAP_US='1000000',
            GATE_WORKERS='2',WORK_CONSERVING='1',WC_AUTO='0',GATE_LEGACY_REPAY='0',GATE_TICK_S='0.1',
            DIAG_DENOMINATOR=denominator,PYTHONPATH='/venv/site-packages',PATH='/venv/bin:/usr/local/bin:/usr/bin:/bin',
            HF_HOME='/hf',HF_HUB_OFFLINE='1',FURIOSA_CACHE_DIR='/furiosa-cache')
        for k,v in env.items(): args += ['--env',k+'='+v]
        args += ['docker.io/library/kraken-npu:qwen',task,'python','-u','/work/npu_diagnostic_entry.py']
        self.save(label+'/input.json', dict(env=env, device_nodes=devices, task_id=task,
                 non_historical_changes=['observer trace and denominator control opt-in','loopback server bind','fresh model context per condition']))
        print(json.dumps(dict(event='start',label=label,budget_us=budget,denominator=denominator)),flush=True)
        self.task=task
        self.log=(out/'container.log').open('w')
        self.process=subprocess.Popen(args,stdout=self.log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('NPU container exited: '+(out/'container.log').read_text()[-5000:])
                try:
                    stats=self.get_stats(port)
                    if all(n in stats for n in ('A','B')):break
                except Exception:pass
                time.sleep(.5)
            else:raise TimeoutError('NPU diagnostic readiness')
            self.save(label+'/process-ownership.json',dict(furiosa_ps=self.cmd(['furiosa-smi','ps'])))
            # Capture a real generated response before the common warmup.
            req=urllib.request.Request(f'http://127.0.0.1:{port}/',data=json.dumps(dict(prompt='What is the capital of France? Answer in one sentence.')).encode(),
                headers={'Content-Type':'application/json','X-Tenant':'A'})
            with urllib.request.urlopen(req,timeout=120) as response: sample=json.load(response)
            if not sample.get('text'):raise RuntimeError('LLM did not return generated text')
            self.save(label+'/actual-response.json',sample)
            self.save(label+'/warmup.json',self.client.load(port,['A','B'],self.warmup))
            before=self.drained_stats(port); began=time.monotonic()
            result=self.client.load(port,['A','B'],self.seconds)
            after=self.drained_stats(port); elapsed=time.monotonic()-began
            if result['errors'] or any(r['done']==0 for r in result['tenants'].values()):
                raise RuntimeError('inference errors or zero completions')
            calculated=summarize_window(before,after)
            for tenant in ['A','B']:
                if calculated[tenant]['deltas']['done'] != result['tenants'][tenant]['done']:
                    raise RuntimeError('gate/client completion counters disagree')
            row=dict(label=label,repeat=repeat,denominator=denominator,budget_us=budget,elapsed_s=elapsed,
                     before=before,after=after,summary=calculated,load=result)
            self.save(label+'/measurement.json',row);self.rows.append(row);self.save('measurements.json',self.rows)
            print(json.dumps(dict(event='result',label=label,summary=calculated,
                  rps=sum(r['rps_drain_inclusive'] for r in result['tenants'].values()))),flush=True)
        finally:
            self.stop_task()

    def run(self):
        try:
            self.prepare()
            arms=[('original',300000,'accepted'),('denominator-only',300000,'nominal'),('budget-only',200000,'accepted')]
            for i in range(self.repeats):
                for name,budget,denominator in arms[i%3:]+arms[:i%3]:
                    self.condition(i+1,name,budget,denominator)
        finally:
            self.stop_task()
            self.save('after.json',dict(processes=self.cmd(['furiosa-smi','ps']),gpu=self.cmd(['nvidia-smi','-L']),
                allocatable=self.cmd(['kubectl','get','node','gpu-npu-server-02','-o','jsonpath={.status.allocatable}']),
                production_configs_modified=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--seconds',type=int,default=40)
    p.add_argument('--warmup',type=int,default=20);p.add_argument('--repeats',type=int,default=3)
    a=p.parse_args();Experiment(a.out,a.seconds,a.warmup,a.repeats).run()
