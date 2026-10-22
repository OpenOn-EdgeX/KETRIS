"""Node supervisor. The 10ms feeder never waits on NVML or the Kubernetes API."""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from .bindings import reconcile_bindings,runtime_info
from .contracts import AllocationBindingV1,utc_now,device_uuid
from .feeder import PartitionController,send_command
from .inventory import read_inventory
from .mps import MpsManager
from .state import atomic_json,read_json

LOG=logging.getLogger('ketris-mig')

class ClusterApi:
    def __init__(self,node,namespace):
        from kubernetes import client,config
        try: config.load_incluster_config()
        except config.ConfigException: config.load_kube_config()
        self.api=client.CoreV1Api();self.node=node;self.namespace=namespace
        self.serialize=client.ApiClient().sanitize_for_serialization

    def pods(self):
        return self.serialize(self.api.list_pod_for_all_namespaces(field_selector='spec.nodeName='+self.node,_request_timeout=3))['items']

    def publish(self,node,value):
        from kubernetes.client.exceptions import ApiException
        for _ in range(3):
            cm=self.api.read_namespaced_config_map('ketris-mig-inventory',self.namespace,_request_timeout=2)
            catalog=json.loads((cm.data or {}).get('catalog.json','{"nodes":{}}'))
            catalog.setdefault('nodes',{})[node]=value
            cm.data={'catalog.json':json.dumps(catalog,allow_nan=False)}
            try:
                self.api.replace_namespaced_config_map('ketris-mig-inventory',self.namespace,cm,_request_timeout=2);return
            except ApiException as e:
                if e.status!=409: raise
        raise RuntimeError('MIG catalog update conflict')

class NodeRuntime:
    def __init__(self,node,root,api,inventory_reader=read_inventory,manager_factory=MpsManager,spawn=subprocess.Popen,allowlist=None):
        self.node=node;self.root=Path(root);self.api=api;self.inventory_reader=inventory_reader
        self.manager_factory=manager_factory;self.spawn=spawn
        if allowlist is None: allowlist=[v.strip() for v in os.environ.get('KETRIS_MIG_ALLOWLIST','').split(',') if v.strip()]
        self.allowlist={device_uuid(v) for v in allowlist}
        self.controllers={};self.managers={};self.children={};self.configs={};self.lock=threading.RLock()
        self.status='starting';self.stop_event=threading.Event();self.plugin=os.environ.get('KETRIS_MIG_PLUGIN','/usr/local/bin/ketris-mig-plugin')
        self.lib_path=os.environ.get('KETRIS_MIG_LIB',str(self.root/'lib/libbless.so'))
        self.cuda_lib=os.environ.get('KETRIS_CUDA_LIB','/usr/local/cuda-12.9/targets/x86_64-linux/lib')
        self.socket_dir=os.environ.get('KETRIS_PLUGIN_SOCKETS','/var/lib/kubelet/device-plugins')
        self.checkpoint_path=Path(self.socket_dir)/'kubelet_internal_checkpoint'
        self.kubelet_generation=None
        self.retired=set()

    def retire(self,key,pods):
        """Disarm removed domains; retain only MPS with verified running owners."""
        with self.lock:
            self.retired.add(key)
            controller=self.controllers[key];controller.reconcile([])
            path,cfg=self.configs[key];cfg['active']=False;atomic_json(path,cfg)
            child=self.children.pop(key,None)
        if child and child.poll() is None:child.terminate();child.wait(timeout=3)
        owners={(p['metadata']['uid'],s['name'],s.get('containerID')) for p in pods
                for s in p.get('status',{}).get('containerStatuses',[]) if 'running' in s.get('state',{})}
        for path in (self.root/'bindings').glob('*.json'):
            binding=read_json(path)
            if (binding.get('mig_uuid'),binding.get('generation'))!=(controller.entry.mig_uuid,controller.entry.generation):continue
            verified=binding.get('phase')=='bound' or binding.get('phase')=='failed' and binding.get('actual_cuda_uuid')==controller.entry.mig_uuid
            if verified and (binding.get('pod_uid'),binding.get('container'),binding.get('container_id')) in owners:return
        # Only this owned manager is stopped, outside the feeder lock.
        self.managers[key].stop(0)
        with self.lock:
            self.controllers.pop(key);self.managers.pop(key);self.configs.pop(key);self.retired.discard(key)
            if not any(c.entry.resource_name==controller.entry.resource_name for c in self.controllers.values()):
                (self.root/'partitions'/(controller.entry.resource_name.split('/')[-1]+'.json')).unlink(missing_ok=True)

    def checkpoint(self): return read_json(self.checkpoint_path)

    def config(self,entry,manager,active=True):
        return {'node':self.node,'gpu_uuid':entry.gpu_uuid,'mig_uuid':entry.mig_uuid,'generation':entry.generation,
                'resource_name':entry.resource_name,'state_dir':str(self.root),'pipe_dir':str(manager.pipe),
                'lib_path':self.lib_path,'cuda_lib_dir':self.cuda_lib,'units':200,'active':active}

    def refresh(self):
        try:
            inventory=[e for e in self.inventory_reader() if e.mig_uuid in self.allowlist];pods=self.api.pods();cp=self.checkpoint()
            ready=[]
            try:
                stat=(Path(self.socket_dir)/'kubelet.sock').stat();kgen=(stat.st_dev,stat.st_ino)
            except FileNotFoundError:kgen=None
            for entry in inventory:
                key=entry.mig_uuid+':'+entry.generation
                self.retired.discard(key)
                if key not in self.managers: self.managers[key]=self.manager_factory(entry,self.root)
                manager=self.managers[key].ensure()
                if key not in self.controllers: self.controllers[key]=PartitionController(self.node,entry)
                config=self.config(entry,manager);path=self.root/'plugins'/(entry.resource_name.split('/')[-1]+'-'+entry.generation+'.json')
                atomic_json(path,config);self.configs[key]=(path,config);ready.append(key)
                child=self.children.get(key)
                if child and self.kubelet_generation and kgen!=self.kubelet_generation:
                    child.terminate();child.wait(timeout=3);child=None
                if child is None or child.poll() is not None:
                    self.children[key]=self.spawn([self.plugin,'--config',str(path),'--socket-dir',self.socket_dir])
            self.kubelet_generation=kgen
            allocations=[];prior={}
            for path in (self.root/'allocations').glob('*.json'):
                if (self.root/'tombstones'/path.stem).exists(): continue
                allocations.append(AllocationBindingV1.from_dict(read_json(path)))
                prior_path=self.root/'bindings'/path.name
                if prior_path.exists(): prior[path.stem]=read_json(prior_path)
            bindings=reconcile_bindings(allocations,cp,pods,inventory,self.root,prior)
            per_partition={k:[] for k in ready}
            for b in bindings:
                value=b.to_dict();old=prior.get(b.allocation_id,{})
                # Keep the original UID once assigned, including failed/terminating state.
                if not value.get('pod_uid') and old.get('pod_uid'):
                    value.update({k:old.get(k) for k in ['pod_uid','container','container_id','time_weight']})
                verified=old.get('phase')=='bound' or old.get('phase')=='failed' and old.get('actual_cuda_uuid')==b.mig_uuid
                if b.mig_uuid+':'+b.generation not in ready and b.phase!='completed' and verified:
                    value={**old,'phase':'failed','updated_at':utc_now()}
                if b.phase=='bound':
                    info,sock=runtime_info(b)
                    value.update(socket=sock,runtime_generation=info['runtime_generation'],reported_sm_count=info['reported_sm_count'],
                                 actual_cuda_uuid=info['actual_cuda_uuid'],runtime_observed_at_us=info['observed_at_us'])
                    per_partition[b.mig_uuid+':'+b.generation].append(value)
                atomic_json(self.root/'bindings'/(b.allocation_id+'.json'),value)
                if b.phase=='completed': atomic_json(self.root/'tombstones'/b.allocation_id,{'at':utc_now()})
            with self.lock:
                for key,controller in self.controllers.items():
                    controller.reconcile(per_partition.get(key,[]))
                    if key not in ready and key in self.configs:
                        path,cfg=self.configs[key];cfg['active']=False;atomic_json(path,cfg)
            for key in list(self.controllers):
                if key not in ready:self.retire(key,pods)
            self.status='ok'
            catalog={'observed_at':utc_now(),'status':'ok','instances':[e.to_dict() for e in inventory]}
            atomic_json(self.root/'inventory.json',{'nodes':{self.node:catalog}})
            self.api.publish(self.node,catalog)
        except Exception as e:
            self.status='unavailable'
            with self.lock:
                for controller in self.controllers.values(): controller.reconcile([])
            # Fail closed also for new admissions/allocation, not just existing credit.
            for path,cfg in self.configs.values(): cfg['active']=False;atomic_json(path,cfg)
            try: self.api.publish(self.node,{'observed_at':utc_now(),'status':'unavailable','instances':[]})
            except Exception: pass
            LOG.warning('source unavailable: %s',type(e).__name__)

    def sample_and_export(self):
        with self.lock: controllers=list(self.controllers.values())
        for controller in controllers:
            with controller.lock: tenants=[dict(t) for t in controller.tenants.values()]
            for t in tenants:
                try:
                    send_command(t['socket'],'time_stats')
                    path=Path(t['control_dir'])/'feed.log'
                    if not path.exists() or time.time()-path.stat().st_mtime>2: continue
                    # Read only the last bounded tail of the append-only file.
                    with path.open('rb') as f:
                        f.seek(max(0,path.stat().st_size-4096));lines=f.read().decode(errors='replace')
                    matches=re.findall(r'total=(\d+) kernels=(\d+) credit=(-?\d+)',lines)
                    if matches:
                        total,kernels,credit=map(int,matches[-1]);controller.observe(t['allocation_id'],total,kernels,time.monotonic(),credit)
                except (OSError,ValueError): pass
            with self.lock:
                identity=controller.entry.mig_uuid+':'+controller.entry.generation
                if identity not in self.controllers:continue
                if identity in self.retired and any(k not in self.retired and c.entry.resource_name==controller.entry.resource_name for k,c in self.controllers.items()):continue
                value=controller.export_state().to_dict()
                value['manager_state']='retired' if identity in self.retired else 'running' if self.status=='ok' else 'unavailable'
                key=controller.entry.resource_name.split('/')[-1]
                atomic_json(self.root/'partitions'/(key+'.json'),value)

    def run(self):
        def supervisor():
            while not self.stop_event.is_set():
                self.refresh();self.stop_event.wait(2)
        thread=threading.Thread(target=supervisor,daemon=True);thread.start()
        next_tick=time.monotonic();last_sample=0
        try:
            while not self.stop_event.is_set():
                with self.lock: controllers=list(self.controllers.values())
                for controller in controllers: controller.tick()
                now=time.monotonic()
                if now-last_sample>=.2:
                    self.sample_and_export();last_sample=now
                # Skip missed rounds; do not burst accumulated wall-clock grants.
                next_tick=max(next_tick+.01,time.monotonic());self.stop_event.wait(max(0,next_tick-time.monotonic()))
        finally:
            with self.lock:
                for controller in self.controllers.values(): controller.reconcile([])
            for child in self.children.values():
                if child.poll() is None: child.terminate()
            # Owned host MPS units intentionally survive supervisor/Pod restart.

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--node',default=os.environ.get('NODE_NAME',os.uname().nodename))
    parser.add_argument('--state-dir',default='/var/lib/ketris-mig');parser.add_argument('--namespace',default='kube-system')
    args=parser.parse_args();logging.basicConfig(level=logging.INFO)
    runtime=NodeRuntime(args.node,args.state_dir,ClusterApi(args.node,args.namespace))
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:runtime.stop_event.set())
    runtime.run()

if __name__=='__main__': main()
