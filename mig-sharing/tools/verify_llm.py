#!/usr/bin/env python3
"""Owned, reversible same-MIG LLM experiment; never changes production images.

Actual K8s Allocate/CDI and existing NodeRuntime/PartitionController are used.
The active legacy admission webhook does not validate MIG: this harness does.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from ketris_mig.contracts import ShareRequest, utc_now
from ketris_mig.inventory import read_inventory
from ketris_mig.llm_experiment import cas_node_catalog, summarize_batches, summarize_requests
from ketris_mig.manifest import build_manifest
from ketris_mig.service import ClusterApi, NodeRuntime
from ketris_mig.state import atomic_json
from ketris_mig.feeder import send_command

ROOT = Path(__file__).resolve().parents[2]
NODE = 'gpu-npu-server-02'
MODEL_REVISION = '7ae557604adf67be50417f59c2c2f167def9a775'
MODEL_RELATIVE = 'models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/' + MODEL_REVISION


class OwnedApi(ClusterApi):
    def __init__(self, node):
        super().__init__(node, 'kube-system')
        cm = self.api.read_namespaced_config_map('ketris-mig-inventory', self.namespace)
        self.uid = cm.metadata.uid
        self.original = json.loads(cm.data['catalog.json']).get('nodes', {}).get(node)
        self.expected = self.original
        self.lock = threading.Lock()

    def publish(self, node, value):
        from kubernetes.client.exceptions import ApiException
        with self.lock:
            for _ in range(3):
                cm = self.api.read_namespaced_config_map('ketris-mig-inventory', self.namespace, _request_timeout=3)
                if cm.metadata.uid != self.uid:
                    raise RuntimeError('catalog ConfigMap was replaced')
                current = json.loads(cm.data['catalog.json'])
                changed = cas_node_catalog(current, node, self.expected, value)
                cm.data['catalog.json'] = json.dumps(changed)
                try:
                    self.api.replace_namespaced_config_map('ketris-mig-inventory', self.namespace, cm, _request_timeout=3)
                    self.expected = value
                    return
                except ApiException as e:
                    if e.status != 409:
                        raise
            raise RuntimeError('catalog contention')

    def restore(self):
        self.publish(self.node, self.original)


class Experiment:
    def __init__(self, out, image, repeats):
        self.out = out.resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.run_id = self.out.name.lower().replace('_', '-')[-40:]
        self.ns = 'ketris-llm-' + hashlib.sha256(self.run_id.encode()).hexdigest()[:8]
        self.state = Path('/var/lib/ketris-llm-lab') / self.run_id
        self.image = image
        self.repeats = repeats
        self.done = threading.Event()
        self.pause = threading.Event()
        self.threads = []
        self.runtime = None
        self.api = None
        self.gi_id = None
        self.gi_handle = None
        self.ci_handle = None
        self.entry = None
        self.mig_enabled = False
        self.cordoned = False
        self.node_claimed = False
        self.baseline_active = False
        self.baseline_stopped = False
        self.cdi_path = Path('/var/run/cdi') / ('ketris-llm-' + self.run_id + '.json')
        self.rows = []
        self.namespace_uid = None
        self.current_directory = None
        self.feed_lock = threading.Lock()
        self.refresh_lock = threading.Lock()

    def save(self, name, value):
        atomic_json(self.out / name, value)

    def cmd(self, args, timeout=120, **kw):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
        with (self.out / 'commands.log').open('a') as f:
            f.write('$ ' + ' '.join(args) + '\n' + r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError('command failed: ' + ' '.join(args) + '\n' + r.stderr[-2000:])
        return r.stdout

    def get(self, kind, name=None, namespace=None):
        return json.loads(self.cmd(['kubectl', 'get', kind] + ([name] if name else []) +
                                  (['-n', namespace] if namespace else ['-A'] if kind == 'pods' else []) + ['-o', 'json']))

    def wait(self, fn, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = fn()
            if result:
                return result
            time.sleep(.1)
        raise TimeoutError('experiment condition timed out')

    def preflight(self):
        if self.get_namespace_exists():
            raise RuntimeError('experiment namespace already exists')
        if self.state.exists() and any(self.state.iterdir()):
            raise RuntimeError('experiment state root already exists')
        gpu = self.cmd(['nvidia-smi', '--query-gpu=uuid,mig.mode.current', '--format=csv,noheader']).strip()
        if not gpu.endswith('Disabled'):
            raise RuntimeError('only an idle initially MIG-disabled GPU is supported')
        self.gpu_uuid = gpu.split(',')[0].strip()
        apps = self.cmd(['nvidia-smi', '--query-compute-apps=pid,process_name', '--format=csv,noheader'])
        if any('mps-server' not in line for line in apps.splitlines() if line.strip()):
            raise RuntimeError('external GPU context present')
        self.before_node = self.get('node', NODE)
        if 'ketris.re.kr/llm-lab' in self.before_node['metadata'].get('annotations', {}):
            raise RuntimeError('another experiment owns the node')
        self.save('before-node.json', self.before_node)
        pods = self.get('pods')
        for p in pods['items']:
            if p.get('status', {}).get('phase') not in ('Pending', 'Running'):
                continue
            for c in p['spec']['containers']:
                if any(k.startswith(('keti.re.kr/lsu', 'keti.re.kr/mig-', 'nvidia.com/')) and str(v) != '0'
                       for k, v in c.get('resources', {}).get('limits', {}).items()):
                    raise RuntimeError('existing GPU Pod: ' + p['metadata']['name'])
        self.save('before-core.json', self.get('ds', 'kraken-device-plugin-lsu', 'kube-system'))
        self.save('before-webhook.json', self.get('deploy', 'kraken-webhook', 'kube-system'))
        self.save('before-runtime.json', self.get('ds', 'ketris-mig-runtime', 'kube-system'))
        if self.get('ds', 'ketris-mig-runtime', 'kube-system')['status'].get('desiredNumberScheduled', 0):
            raise RuntimeError('another MIG runtime is scheduled')
        self.cdi_before = Path('/var/run/cdi/nvidia.yaml').read_bytes()
        (self.out / 'before-cdi.yaml').write_bytes(self.cdi_before)
        if self.cdi_path.exists():
            raise RuntimeError('experiment CDI path exists')
        self.baseline_active = subprocess.run(['systemctl', 'is-active', 'nvidia-mps'], capture_output=True, text=True).stdout.strip() == 'active'
        if self.baseline_active:
            env = dict(os.environ, CUDA_MPS_PIPE_DIRECTORY='/tmp/nvidia-mps')
            for pid in self.cmd(['nvidia-cuda-mps-control'], input='get_server_list\n', env=env).split():
                clients = self.cmd(['nvidia-cuda-mps-control'], input='get_client_list ' + pid + '\n', env=env).strip()
                if clients:
                    raise RuntimeError('external MPS clients: ' + clients)
        self.api = OwnedApi(NODE)
        self.save('before-catalog.json', self.api.original)
        self.save('image.json', json.loads(self.cmd(['docker', 'image', 'inspect', self.image]))[0])
        # Atomically reserve new scheduling while preserving existing workloads.
        patch = [dict(op='test', path='/metadata/resourceVersion', value=self.before_node['metadata']['resourceVersion'])]
        annotations = dict(self.before_node['metadata'].get('annotations', {}))
        annotations['ketris.re.kr/llm-lab'] = self.run_id
        patch.append(dict(op='add', path='/metadata/annotations', value=annotations))
        if not self.before_node['spec'].get('unschedulable', False):
            patch.append(dict(op='add', path='/spec/unschedulable', value=True))
        self.cmd(['kubectl', 'patch', 'node', NODE, '--type=json', '-p', json.dumps(patch)])
        self.node_claimed = True
        self.cordoned = not self.before_node['spec'].get('unschedulable', False)

    def setup(self):
        # Recheck after claiming the node: assignment could have raced preflight.
        apps = self.cmd(['nvidia-smi', '--query-compute-apps=pid,process_name', '--format=csv,noheader'])
        if any('mps-server' not in line for line in apps.splitlines() if line.strip()):
            raise RuntimeError('GPU became busy after reservation')
        for p in self.api.pods():
            if p.get('status', {}).get('phase') not in ('Running', 'Pending'):
                continue
            if any(any(k.startswith(('keti.re.kr/lsu', 'keti.re.kr/mig-', 'nvidia.com/')) and str(v) != '0'
                       for k, v in c.get('resources', {}).get('limits', {}).items()) for c in p['spec']['containers']):
                raise RuntimeError('GPU Pod assigned during reservation')
        if self.baseline_active:
            env = dict(os.environ, CUDA_MPS_PIPE_DIRECTORY='/tmp/nvidia-mps')
            for pid in self.cmd(['nvidia-cuda-mps-control'], input='get_server_list\n', env=env).split():
                if self.cmd(['nvidia-cuda-mps-control'], input='get_client_list ' + pid + '\n', env=env).strip():
                    raise RuntimeError('baseline MPS acquired clients during reservation')
        if self.baseline_active:
            self.cmd(['systemctl', 'stop', 'nvidia-mps'])
            self.baseline_stopped = True
        self.cmd(['nvidia-smi', '-i', self.gpu_uuid, '-mig', '1'])
        self.mig_enabled = True
        # NVML returns the newly created GI handle; journal before any CI/CDI step.
        import pynvml as n
        n.nvmlInit()
        try:
            parent = n.nvmlDeviceGetHandleByUUID(self.gpu_uuid)
            profiles = []
            for i in range(32):
                try:
                    profiles.append(n.nvmlDeviceGetGpuInstanceProfileInfo(parent, i))
                except (n.NVMLError_InvalidArgument, n.NVMLError_NotSupported):
                    pass
            profile = next(p for p in profiles if p.id == 14)
            gi = n.nvmlDeviceCreateGpuInstance(parent, profile.id)
            self.gi_handle = gi
            self.gi_id = int(n.nvmlGpuInstanceGetInfo(gi).id)
            self.save('created-gi.json', dict(gpu_uuid=self.gpu_uuid, gi_id=self.gi_id))
            cp = n.nvmlGpuInstanceGetComputeInstanceProfileInfo(gi, 0, 0)
            self.ci_handle = n.nvmlGpuInstanceCreateComputeInstance(gi, cp.id)
        finally:
            n.nvmlShutdown()
        inventory = read_inventory()
        if len(inventory) != 1 or inventory[0].gi_id != self.gi_id:
            raise RuntimeError('unexpected MIG geometry')
        self.entry = inventory[0]
        self.save('inventory.json', self.entry.to_dict())
        cdi = json.loads(self.cmd(['nvidia-ctk', 'cdi', 'generate', '--format=json']))
        cdi['devices'] = [d for d in cdi['devices'] if d['name'] == self.entry.mig_uuid]
        if len(cdi['devices']) != 1:
            raise RuntimeError('CDI lacks exact MIG UUID')
        self.cdi_bytes = json.dumps(cdi).encode()
        self.cdi_path.write_bytes(self.cdi_bytes)
        self.runtime = NodeRuntime(NODE, self.state, self.api, allowlist=[self.entry.mig_uuid])
        self.runtime.plugin = str(ROOT / '.build/ketris-mig-plugin')
        self.runtime.lib_path = str(ROOT / '.build/runtime/libbless.so')
        self.runtime.refresh()
        if self.runtime.status != 'ok':
            raise RuntimeError('owned MIG runtime did not initialize')
        def refresh():
            while not self.done.wait(.2):
                with self.refresh_lock:
                    if self.pause.is_set():
                        continue
                    self.runtime.sample_and_export()
                    directory = self.current_directory
                    if directory:
                        sample = dict(monotonic_ns=time.monotonic_ns(), captured_at=utc_now(),
                                      partitions=[c.export_state().to_dict() for c in self.runtime.controllers.values()])
                        with (directory / 'runtime-samples.jsonl').open('a') as f:
                            f.write(json.dumps(sample) + '\n')
                    if time.monotonic() - getattr(self, 'last_refresh', 0) > 1:
                        self.runtime.refresh()
                        self.last_refresh = time.monotonic()
        def feed():
            next_tick = time.monotonic()
            while not self.done.is_set():
                with self.feed_lock:
                    if not self.pause.is_set():
                        for controller in list(self.runtime.controllers.values()):
                            controller.tick()
                next_tick = max(next_tick + .01, time.monotonic())
                self.done.wait(max(0, next_tick-time.monotonic()))
        for fn in (refresh, feed):
            t = threading.Thread(target=fn, daemon=True)
            t.start(); self.threads.append(t)
        self.wait(lambda: self.get('node', NODE)['status']['allocatable'].get(self.entry.resource_name) == '200')
        self.cmd(['kubectl', 'create', 'namespace', self.ns])
        self.namespace_uid = self.get('namespace', self.ns)['metadata']['uid']
        self.save('execution-boundary.json', dict(workloads='real K8s Pods with exact MIG CDI Allocate',
              controller='owned host NodeRuntime / PartitionController, same code as runtime DaemonSet',
              production_images_changed=False, admission='harness-local validation; active legacy webhook ignores MIG',
              model='Qwen/Qwen2.5-0.5B-Instruct', model_revision=MODEL_REVISION,
              model_files='host cache mounted read-only; not embedded in image', attention='eager',
              token_policy='32 greedy tokens, EOS ignored for a fixed compute budget',
              time_policy='strict, no unused-credit borrowing, 1 second credit bank; kernel launches are nonpreemptive'))

    def bindings(self):
        result = {}
        for path in (self.state / 'bindings').glob('*.json'):
            b = json.loads(path.read_text())
            if b.get('phase') == 'bound':
                result[b['pod_uid']] = b
        return result

    def pause_control(self):
        self.pause.set()
        # Acknowledge completion of in-flight tick/reconcile before resetting credit.
        with self.feed_lock:
            pass
        with self.refresh_lock:
            pass

    def stats(self, binding):
        send_command(binding['socket'], 'time_stats')
        time.sleep(.05)
        text = (Path(binding['control_dir']) / 'feed.log').read_text()[-8192:]
        match = re.findall(r'total=(\d+) kernels=(\d+) credit=(-?\d+)', text)[-1]
        return dict(zip(('charged_us', 'kernels', 'credit_us'), map(int, match)))

    def pod(self, directory, role, policy):
        pod = build_manifest(ShareRequest(self.entry.mig_uuid, 50, 3 if role == 'A' else 1),
                             self.image, self.ns, ['python', '-u', '/opt/llm/llm_launch.py'], [self.entry])
        pod['metadata']['name'] = 'llm-' + role.lower()
        pod['spec'].update(nodeName=NODE, terminationGracePeriodSeconds=5)
        pod['spec']['volumes'] = [dict(name='lab', hostPath=dict(path=str(directory), type='Directory')),
                                 dict(name='models', hostPath=dict(path='/root/.cache/huggingface/hub', type='Directory')),
                                 dict(name='shm', emptyDir=dict(medium='Memory', sizeLimit='1Gi'))]
        c = pod['spec']['containers'][0]
        c.update(imagePullPolicy='Never')
        c['volumeMounts'] = [dict(name='lab', mountPath='/lab'), dict(name='models', mountPath='/models', readOnly=True),
                             dict(name='shm', mountPath='/dev/shm')]
        c['resources']['requests'].update(cpu='2', memory='4Gi')
        c['resources']['limits']['memory'] = '12Gi'
        values = dict(LLM_DIR='/lab', LLM_ROLE=role, LLM_POLICY=policy, LLM_EXPECTED_MIG=self.entry.mig_uuid,
                      LLM_MODEL_PATH='/models/' + MODEL_RELATIVE,
                      CUDA_MPS_CLIENT_PRIORITY='1' if policy == 'priority' and role == 'B' else '0')
        c['env'] += [dict(name=k, value=v) for k, v in values.items()]
        return pod

    def run_condition(self, label, policy, roles, config=None, probe=False):
        directory = self.out / label
        directory.mkdir()
        self.current_directory = directory
        pod_names = []
        owned_bindings = {}
        try:
            for role in roles:
                pod = self.pod(directory, role, policy)
                path = directory / ('pod-' + role + '.json')
                atomic_json(path, pod)
                self.cmd(['kubectl', 'apply', '-f', str(path)])
                pod_names.append(pod['metadata']['name'])
            def ready():
                for name in pod_names:
                    pod = self.get('pod', name, self.ns)
                    state = pod.get('status', {}).get('containerStatuses', [{}])[0].get('state', {})
                    if 'terminated' in state:
                        raise RuntimeError('worker terminated: ' + self.cmd(['kubectl', 'logs', name, '-n', self.ns]))
                return all((directory / ('ready-' + role + '.json')).exists() for role in roles)
            self.wait(ready)
            for name, role in zip(pod_names, roles):
                pod = self.get('pod', name, self.ns)
                self.save(label + '/admitted-' + role + '.json', pod)
                if policy == 'ketris':
                    uid = pod['metadata']['uid']
                    b = self.wait(lambda: self.bindings().get(uid))
                    owned_bindings[role] = b
            self.save(label + '/bindings.json', owned_bindings)
            if probe:
                self.pause_control()
                b = owned_bindings['A']
                send_command(b['socket'], 'time_credit 0')
                before = self.stats(b)
                atomic_json(directory / 'go.json', dict(probe=True))
                self.wait(lambda: (directory / 'probe-before.json').exists(), 10)
                time.sleep(1)
                if (directory / 'probe-after.json').exists():
                    raise RuntimeError('actual Qwen CUDA path bypassed zero-credit gate')
                blocked = self.stats(b)
                send_command(b['socket'], 'time_add 1000000')
                self.wait(lambda: (directory / 'probe-after.json').exists(), 30)
                after = self.stats(b)
                if after['kernels'] <= blocked['kernels']:
                    raise RuntimeError('LLM progress lacks intercepted kernel evidence')
                result = dict(zero_credit_seconds=1, completed_without_credit=False, completed_after_grant=True,
                              before=before, blocked=blocked, after=after)
                self.save(label + '/gate-proof.json', result)
                print(json.dumps(dict(event='llm_gate_proof', **result)), flush=True)
                return result
            self.pause_control()
            for b in owned_bindings.values():
                send_command(b['socket'], 'time_credit 0')
            before = {role: self.stats(b) for role, b in owned_bindings.items()}
            self.pause.clear()
            start = time.monotonic_ns() + 1_000_000_000
            go = dict(config, start_ns=start, end_ns=start+config['requests']*config['interval_ns'])
            atomic_json(directory / 'go.json', go)
            self.wait(lambda: all((directory / ('result-' + role + '.json')).exists() for role in roles),
                      max(120, config['requests']*config['interval_ns']/1e9*8))
            after = {role: self.stats(b) for role, b in owned_bindings.items()}
            workers = {role: json.loads((directory / ('result-' + role + '.json')).read_text()) for role in roles}
            result = dict(label=label, policy=policy, config=go, mig=self.entry.to_dict(),
                          A=summarize_requests(workers['A']['requests'], config['ttft_deadline_ms'], config['completion_deadline_ms']),
                          B=summarize_batches(workers['B']['batches'], start, go['end_ns']) if 'B' in workers else None,
                          runtime_before=before, runtime_after=after,
                          identities={role: w['identity'] for role, w in workers.items()})
            self.save(label + '/summary.json', result)
            self.save(label + '/partition-export.json', self.runtime.controllers[self.entry.mig_uuid+':'+self.entry.generation].export_state().to_dict())
            print(json.dumps(dict(event='condition_complete', label=label, A=result['A'], B=result['B'])), flush=True)
            self.rows.append(result)
            return result
        finally:
            self.current_directory = None
            self.pause.clear()
            (directory / 'stop').touch()
            for name in pod_names:
                try:
                    (directory / (name + '.log')).write_text(self.cmd(['kubectl', 'logs', name, '-n', self.ns]))
                    self.cmd(['kubectl', 'delete', 'pod', name, '-n', self.ns, '--ignore-not-found', '--timeout=60s'])
                except Exception as e:
                    self.save(label + '/cleanup-' + name + '.json', dict(error=str(e)))
            time.sleep(1.2)

    def cleanup(self):
        errors = []
        def attempt(name, fn):
            try:
                fn()
            except Exception as e:
                errors.append(dict(step=name, error=str(e)))
        if self.namespace_uid:
            def delete_namespace():
                if self.get('namespace', self.ns)['metadata']['uid'] != self.namespace_uid:
                    raise RuntimeError('namespace replaced externally')
                from kubernetes import client
                self.api.api.delete_namespace(self.ns, body=client.V1DeleteOptions(
                    preconditions=client.V1Preconditions(uid=self.namespace_uid)), _request_timeout=10)
                self.wait(lambda: not self.get_namespace_exists(), 90)
            attempt('delete owned namespace', delete_namespace)
        self.done.set()
        for t in self.threads:
            t.join(10)
        if any(t.is_alive() for t in self.threads):
            errors.append(dict(step='runtime threads', error='thread still active; unsafe to destroy MIG'))
        if self.runtime:
            def verify_no_clients():
                for p in self.api.pods():
                    if p.get('status', {}).get('phase') not in ('Running', 'Pending'):
                        continue
                    if any(self.entry.resource_name in c.get('resources', {}).get('limits', {}) for c in p['spec']['containers']):
                        raise RuntimeError('MIG Pod still owns resource: ' + p['metadata']['name'])
                for manager in self.runtime.managers.values():
                    env = dict(os.environ, CUDA_MPS_PIPE_DIRECTORY=str(manager.pipe))
                    for pid in self.cmd(['nvidia-cuda-mps-control'], input='get_server_list\n', env=env).split():
                        clients = self.cmd(['nvidia-cuda-mps-control'], input='get_client_list ' + pid + '\n', env=env).strip()
                        if clients:
                            raise RuntimeError('MPS clients remain; retaining GPU: ' + clients)
            attempt('verify MIG clients gone', verify_no_clients)
            if not errors:
                for controller in self.runtime.controllers.values():
                    attempt('disarm', lambda c=controller: c.reconcile([]))
                for process in self.runtime.children.values():
                    attempt('stop owned plugin', lambda p=process: (p.terminate(), p.wait(10)))
                for manager in self.runtime.managers.values():
                    attempt('stop owned MPS', lambda m=manager: m.stop(0))
        if self.cdi_path.exists() and not errors:
            if self.cdi_path.read_bytes() == getattr(self, 'cdi_bytes', None):
                self.cdi_path.unlink()
            else:
                errors.append(dict(step='CDI cleanup', error='owned file changed externally'))
        if self.api:
            attempt('catalog CAS restore', self.api.restore)
        if self.gi_handle is not None and not errors:
            current = read_inventory()
            if self.entry and any(e.gi_id == self.gi_id and e.mig_uuid != self.entry.mig_uuid for e in current):
                errors.append(dict(step='GI cleanup', error='GI identity changed'))
            else:
                def destroy_instances():
                    import pynvml as n
                    n.nvmlInit()
                    try:
                        if self.ci_handle is not None:
                            n.nvmlComputeInstanceDestroy(self.ci_handle)
                        n.nvmlGpuInstanceDestroy(self.gi_handle)
                    finally:
                        n.nvmlShutdown()
                attempt('destroy owned CI/GI', destroy_instances)
        if self.mig_enabled and not errors:
            attempt('restore MIG disabled', lambda: self.cmd(['nvidia-smi', '-i', self.gpu_uuid, '-mig', '0']))
        if self.baseline_stopped and not errors:
            attempt('restore baseline MPS', lambda: self.cmd(['systemctl', 'start', 'nvidia-mps']))
        if self.node_claimed and not errors:
            attempt('restore scheduling', lambda: self.cmd(['kubectl', 'patch', 'node', NODE, '--type=json', '-p',
                json.dumps([dict(op='test', path='/metadata/uid', value=self.before_node['metadata']['uid']),
                            dict(op='test', path='/metadata/annotations/ketris.re.kr~1llm-lab', value=self.run_id),
                            dict(op='test', path='/spec/unschedulable', value=True),
                            dict(op='remove', path='/metadata/annotations/ketris.re.kr~1llm-lab'),
                            *([dict(op='replace', path='/spec/unschedulable', value=False)] if self.cordoned else [])])]))
        if hasattr(self, 'cdi_before') and Path('/var/run/cdi/nvidia.yaml').read_bytes() != self.cdi_before:
            errors.append(dict(step='baseline CDI', error='baseline file changed externally; not overwritten'))
        for filename, kind, name in [('before-core.json','ds','kraken-device-plugin-lsu'), ('before-webhook.json','deploy','kraken-webhook')]:
            if not (self.out / filename).exists():
                continue
            before = json.loads((self.out / filename).read_text())
            now = self.get(kind, name, 'kube-system')
            if before['metadata']['uid'] != now['metadata']['uid'] or before['spec'] != now['spec']:
                errors.append(dict(step=name, error='production spec changed externally; not rolled back'))
        self.save('restoration.json', dict(errors=errors, completed=not errors,
                   gpu_state=self.cmd(['nvidia-smi', '-L']), node=self.get('node', NODE)))
        if errors:
            raise RuntimeError('Restoration needs inspection: ' + json.dumps(errors))

    def get_namespace_exists(self):
        r = subprocess.run(['kubectl', 'get', 'namespace', self.ns, '--ignore-not-found', '-o', 'name'], capture_output=True, text=True, timeout=20)
        if r.returncode:
            raise RuntimeError('namespace lookup unavailable: ' + r.stderr)
        return bool(r.stdout.strip())

    def run(self, pilot_only=False):
        try:
            self.preflight()
            self.setup()
            self.run_condition('gate-probe', 'ketris', ['A'], probe=True)
            pilot = self.run_condition('solo-calibration', 'native', ['A'],
                      dict(requests=8, interval_ns=800_000_000, output_tokens=32, ttft_deadline_ms=1e6, completion_deadline_ms=1e6))
            # Freeze SLO and arrival spacing from solo data before any contention condition.
            config = dict(requests=24, output_tokens=32,
                          interval_ns=max(100_000_000, math.ceil(pilot['A']['completion_ms']['p50']*1.5)*1_000_000),
                          ttft_deadline_ms=math.ceil(pilot['A']['ttft_ms']['p95']*1.5),
                          completion_deadline_ms=math.ceil(pilot['A']['completion_ms']['p95']*1.5))
            self.save('frozen-config.json', config)
            if not pilot_only:
                # Balanced rotation, fresh Pods/contexts each condition, no result-based reordering.
                arms = [('mps', 'native'), ('mps-priority', 'priority'), ('ketris-3to1', 'ketris')]
                for repeat in range(self.repeats):
                    ordered = arms[repeat % 3:] + arms[:repeat % 3]
                    for label, policy in ordered:
                        self.run_condition(f'{label}-r{repeat+1}', policy, ['A', 'B'], config)
            self.save('results.json', self.rows)
        finally:
            self.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--image', default='ketris-llm-workload:20261014-qwen05b-v1')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--pilot-only', action='store_true')
    args = parser.parse_args()
    Experiment(Path(args.out), args.image, args.repeats).run(args.pilot_only)


if __name__ == '__main__':
    main()
