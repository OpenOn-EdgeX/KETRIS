from datetime import datetime,timezone
import csv,json,os,re,subprocess,threading,time
from pathlib import Path
import httpx
from ..models import SourceStatus

def utc():return datetime.now(timezone.utc).isoformat()

class SourceCache:
    def __init__(self,name,interval,provider):
        self.name=name;self.interval=interval;self.provider=provider;self.value=None
        self.last_success=None;self.last_success_time=None;self.error=None;self.lock=threading.RLock()
    def poll(self):
        try:
            value=self.provider()
            with self.lock:self.value=value;self.last_success=utc();self.last_success_time=time.monotonic();self.error=None
        except Exception as e:
            with self.lock:self.error=type(e).__name__
    def read(self):
        with self.lock:
            stale=self.last_success_time is not None and (self.error or time.monotonic()-self.last_success_time>max(3*self.interval,10))
            if not stale and self.name in {'runtime','npu_gates'} and self.value==[]:
                reason='no active MIG runtime export (fresh lookup)' if self.name=='runtime' else 'no bound gate stats endpoint configured'
                return self.value,SourceStatus(status='unavailable',last_success=self.last_success,reason=reason)
            status='stale' if stale else 'ok' if self.last_success_time is not None else 'unavailable'
            return self.value,SourceStatus(status=status,last_success=self.last_success,reason=self.error or ('source cadence exceeded' if stale else None))

def command(args,timeout):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout).stdout

def get_json(url,timeout=2):
    r=httpx.get(url,timeout=timeout);r.raise_for_status();return r.json()

def parse_npu_processes(text):
    result=[]
    for line in text.splitlines():
        m=re.search(r'\|\s*(\d+)\s*\|\s*(npu\d+):\[([^]]+)\]',line)
        if not m:continue
        nums=[int(v) for v in re.findall(r'\d+',m[3])]
        if not nums:continue
        if len(nums)==2 and nums[0]<=nums[1]:pe=list(range(nums[0],nums[1]+1))
        elif len(nums)==1:pe=nums
        else:continue # ambiguous driver syntax cannot invent a range
        result.append({'pid':int(m[1]),'device':m[2],'pe_indices':pe})
    return result

class ReadOnlySources:
    def __init__(self,node,state_root='/var/lib/ketris-mig',proc_root='/host-proc',getter=get_json,runner=command,dev_root='/dev/rngd',gate_endpoints=None):
        self.node=node;self.root=Path(state_root);self.proc_root=Path(proc_root);self.getter=getter;self.runner=runner
        self.dev_root=Path(dev_root)
        self.gate_endpoints=gate_endpoints if gate_endpoints is not None else json.loads(os.environ.get('KETRIS_NPU_GATE_ENDPOINTS','[]'))
        self._api=None;self._serializer=None;self._pods=None;self._resource_version=None
    def gpu(self):
        output=self.runner(['nvidia-smi','--query-gpu=uuid,name,memory.total,memory.used,utilization.gpu,mig.mode.current','--format=csv,noheader,nounits'],3)
        devices=[]
        for row in csv.reader(output.splitlines()):
            if len(row)!=6:raise ValueError('GPU query schema changed')
            uuid,name,total,used,util,mode=[v.strip() for v in row]
            if not re.fullmatch(r'GPU-[0-9a-fA-F-]{36}',uuid):raise ValueError('invalid physical GPU identity')
            def num(v):
                try:return float(v)
                except ValueError:return None
            devices.append(dict(uuid=uuid,name=name,memory_mib=num(total),memory_used_mib=num(used),util_percent=num(util),mig_mode=mode))
        # NVML query lives in a bounded child so a stuck driver cannot freeze observation.
        script='import json;from ketris_mig.inventory import read_inventory;print(json.dumps([e.to_dict() for e in read_inventory()]))'
        migs=json.loads(self.runner(['python3','-c',script],3))
        return {'devices':devices,'migs':migs}
    def npu(self):
        info=self.runner(['furiosa-smi','info'],4);ps=self.runner(['furiosa-smi','ps'],4)
        counts=[int(n) for n in re.findall(r'\b(\d+)\s*PE\b',info,re.I)]
        # RNGD info prints its core range; do not assume8 on an unavailable driver.
        ranges=re.findall(r'npu\d+:\[(\d+),\s*(\d+)\]',info)
        if counts:count=max(counts)
        elif ranges:count=max(int(b)+1 for a,b in ranges)
        else:
            # Query actual named cores in human output (PE0..PE7 / core0..core7).
            cores=[int(v) for v in re.findall(r'(?:PE|core)\s*(\d+)',info,re.I)]
            count=max(cores)+1 if cores else None
        nodes=list(self.dev_root.glob('npu0pe[0-9]*'))
        indices=[int(m[1]) for p in nodes if (m:=re.fullmatch(r'npu0pe(\d+)',p.name))]
        if indices and sorted(indices)==list(range(max(indices)+1)):count=len(indices)
        return {'pe_count':count,'processes':parse_npu_processes(ps)}
    def runtime(self):
        values=[]
        for p in (self.root/'partitions').glob('*.json'):
            if time.time()-p.stat().st_mtime>10:continue
            data=json.loads(p.read_text())
            if data.get('node')==self.node:values.append(data)
        return values
    def controller(self):
        return {'status':self.getter('http://127.0.0.1:8090/status',timeout=2),'capacity':self.getter('http://127.0.0.1:8090/capacity',timeout=2)}
    def npu_gates(self):
        values=[]
        allowed={'http://127.0.0.1:18180/stats','http://127.0.0.1:18181/stats'}
        fields={'armed','unlimited','credit_us','granted_us','charged_us','charged_extra_us','done','done_extra','queue','weight','ratio'}
        for endpoint in self.gate_endpoints:
            if endpoint['url'] not in allowed or not endpoint.get('pod_uid') or not endpoint.get('container'):raise ValueError('gate endpoint is not allowlisted/bound')
            raw=self.getter(endpoint['url'],timeout=2)
            tenants={name:{k:v for k,v in t.items() if k in fields and isinstance(v,(int,float,bool))} for name,t in raw.get('tenants',{}).items()}
            values.append({'pod_uid':endpoint['pod_uid'],'container':endpoint['container'],'url':endpoint['url'],'tenants':tenants})
        return values
    def api(self):
        if self._api is None:
            from kubernetes import client,config
            try:config.load_incluster_config()
            except config.ConfigException:config.load_kube_config()
            self._api=client.CoreV1Api();self._serializer=client.ApiClient().sanitize_for_serialization
        return self._api
    def kubernetes(self):
        api=self.api();data=self._serializer(api.list_pod_for_all_namespaces(field_selector='spec.nodeName='+self.node,_request_timeout=3))
        self._pods=data['items'];self._resource_version=data['metadata']['resourceVersion']
        cp=self.checkpoint()
        return {'pods':self._pods,'checkpoint':cp}
    def checkpoint(self):return json.loads(Path('/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint').read_text())
    def watch_pods(self,cache,stop):
        # Event updates reduce the 30s resync delay; no Kubernetes write calls exist.
        from kubernetes import watch
        while not stop.is_set():
            try:
                api=self.api();watcher=watch.Watch()
                for event in watcher.stream(api.list_pod_for_all_namespaces,field_selector='spec.nodeName='+self.node,resource_version=self._resource_version,timeout_seconds=25,_request_timeout=30):
                    if stop.is_set():watcher.stop();return
                    pod=self._serializer(event['object']);uid=pod['metadata']['uid'];self._resource_version=pod['metadata']['resourceVersion']
                    with cache.lock:
                        if cache.value is None:continue
                        pods=[p for p in cache.value['pods'] if p['metadata']['uid']!=uid]
                        if event['type']!='DELETED':pods.append(pod)
                        cache.value={**cache.value,'pods':pods};cache.last_success=utc();cache.last_success_time=time.monotonic();cache.error=None
            except Exception:stop.wait(2)
