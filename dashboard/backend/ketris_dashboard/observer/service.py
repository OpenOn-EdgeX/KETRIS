import argparse,json,logging,os,signal,threading,time,uuid
from pathlib import Path
import httpx
from ..models import Device,Partition,Workload,ViewSnapshotV1,SourceStatus,observed
from ..normalize import normalize_runtime,CounterPoint
from .sources import SourceCache,ReadOnlySources,utc
from .bindings import checkpoint_workloads,pid_owner,pending_workloads
from .counters import CounterHistory

class NodeObserver:
    def __init__(self,node,sources,proc_root='/host-proc'):
        self.node=node;self.sources=sources;self.proc_root=Path(proc_root);self.counters=CounterHistory()
    def collect_node(self):
        results={k:c.read() for k,c in self.sources.items()};data={k:v for k,(v,s) in results.items() if v is not None}
        statuses={k:s for k,(v,s) in results.items()};at=utc();snap=ViewSnapshotV1(node=self.node,captured_at=at,source_status=statuses)
        def fresh(*keys):return all(statuses.get(k,SourceStatus(status='unavailable')).status=='ok' for k in keys)
        def stamp(workloads,keys,groups):
            times=[statuses[k].last_success for k in keys if k in statuses and statuses[k].last_success]
            joined_at=min(times) if len(times)==len(keys) else None
            for w in workloads:
                if not fresh(*keys):w.state='stale'
                for group in groups:
                    for value in getattr(w,group).values():
                        value.observed_at=joined_at
                        if value.value is not None and not fresh(*keys):value.quality='stale';value.reason='contributing source stale or unavailable'
        def metric(value,unit,source,source_key,quality='observed',reason=None):
            status=statuses.get(source_key)
            return observed(value,unit,source,status.last_success if status else None,'stale' if status and status.status=='stale' and value is not None else quality,reason)
        gpu=data.get('gpu',{})
        for d in gpu.get('devices',[]):
            id=self.node+':'+d['uuid']
            snap.devices.append(Device(id=id,kind='gpu',name=d['name'],node=self.node,metrics={k:metric(d.get(k),unit,'gpu_driver','gpu') for k,unit in [('memory_mib','MiB'),('memory_used_mib','MiB'),('util_percent','%'),('mig_mode',None)]}))
            if d.get('mig_mode')=='Disabled':snap.partitions.append(Partition(id=id+':full',device_id=id,kind='gpu',name='Full GPU · MIG Disabled'))
        for m in gpu.get('migs',[]):
            snap.partitions.append(Partition(id=self.node+':'+m['mig_uuid'],device_id=self.node+':'+m['gpu_uuid'],kind='mig',name=m.get('profile') or 'MIG',generation=m['generation'],metrics={
                'mig_uuid':metric(m['mig_uuid'],None,'gpu_driver','gpu'),'profile_sm':metric(m.get('profile_sm'),'SM','gpu_driver','gpu'),'memory_mib':metric(m.get('memory_mib'),'MiB','gpu_driver','gpu')}))
        kube=data.get('kubernetes',{});pods=kube.get('pods',[])
        by_uid={p['metadata']['uid']:p for p in pods}
        points={};sample_at=time.monotonic();invalid_membership=set()
        for state in data.get('runtime',[]):
            # Do not resurrect exports for a MIG that the current driver removed.
            valid_migs={m['mig_uuid'] for m in gpu.get('migs',[])}
            if statuses.get('gpu') and statuses['gpu'].status=='ok' and state['mig_uuid'] not in valid_migs:continue
            try:runtime=normalize_runtime(state)
            except ValueError:continue
            for p in runtime.partitions:
                if p.id not in {x.id for x in snap.partitions}:snap.partitions.append(p)
            for w,t in zip(runtime.workloads,state['tenants']):
                pod=by_uid.get(w.pod_uid)
                if statuses.get('kubernetes') and statuses['kubernetes'].status=='ok' and pod is None:continue
                if pod:w.name=pod['metadata']['name'];w.namespace=pod['metadata'].get('namespace')
                stale=not fresh('runtime','kubernetes','gpu') or state['manager_state']!='running'
                if stale:invalid_membership.add(state['budget_domain']);w.state='stale'
                for group in [w.requested,w.applied,w.observed]:
                    for value in group.values():
                        if stale and value.value is not None:value.quality='stale';value.reason='runtime export stale'
                # Observer computes its own complete-window deltas; no occupancy POST.
                ts=Path(t['control_dir'])/'feed.log'
                import re
                total=None
                try:
                    if time.time()-ts.stat().st_mtime<=2:
                        with ts.open('rb') as f:f.seek(max(0,ts.stat().st_size-4096));text=f.read().decode(errors='replace')
                        matches=re.findall(r'total=(\d+) kernels=(\d+)',text)
                        if matches:total=int(matches[-1][0])
                except OSError:pass
                points[w.id]=CounterPoint(total,sample_at,t.get('runtime_generation','unknown'),state['budget_domain'])
                snap.workloads.append(w)
        base=checkpoint_workloads(self.node,data.get('checkpoint',kube.get('checkpoint',{})),pods)
        checkpoint_key='checkpoint' if 'checkpoint' in self.sources else 'kubernetes'
        stamp(base,['kubernetes'],['requested'])
        stamp(base,list(dict.fromkeys([checkpoint_key,'kubernetes'])),['applied'])
        snap.workloads+=base
        pending=pending_workloads(self.node,pods,snap.workloads,gpu.get('migs',[]));stamp(pending,['kubernetes'],['requested']);snap.workloads+=pending
        for gate in data.get('npu_gates',[]):
            owner=next((w for w in base if (w.pod_uid,w.container)==(gate['pod_uid'],gate['container'])),None)
            if owner is None:continue
            pod=by_uid.get(owner.pod_uid);status=next((s for s in pod.get('status',{}).get('containerStatuses',[]) if s['name']==owner.container),{})
            if 'running' not in status.get('state',{}):continue
            for tenant,t in gate['tenants'].items():
                w=owner.model_copy(deep=True);w.id+=':gate:'+tenant;w.name+=' / '+tenant;w.state='gate observed'
                w.applied['time_share']=metric(t.get('ratio'),'fraction','runtime','npu_gates','configured')
                w.observed={'charged_total_us':metric(t.get('charged_us'),'us','runtime','npu_gates'),
                    'requests_done':metric(t.get('done'),'requests','runtime','npu_gates'),'charged_extra_us':metric(t.get('charged_extra_us'),'us','runtime','npu_gates'),
                    'credit_us':metric(t.get('credit_us'),'us','runtime','npu_gates')}
                generation=status.get('containerID') or 'unavailable'
                domain=w.id.rsplit(':gate:',1)[0]
                if not fresh('npu_gates','kubernetes',checkpoint_key):invalid_membership.add(domain);w.state='stale'
                points[w.id]=CounterPoint(t.get('charged_us'),sample_at,generation,domain);snap.workloads.append(w)
        # A fresh log cannot prove that a stale member list is the whole denominator.
        points={id:CounterPoint(None if point.domain in invalid_membership else point.value,point.at,point.generation,point.domain) for id,point in points.items()}
        shares=self.counters.update(points)
        for w in snap.workloads:
            point=points.get(w.id)
            if point:
                value=shares[point.domain].shares.get(w.id)
                reason='membership source stale or unavailable' if point.domain in invalid_membership else 'observer delta of fresh complete same-domain counters'
                w.observed['charged_share']=observed(value,'fraction','runtime',at if value is not None else None,'derived',reason)
        npu=data.get('npu')
        if npu is not None:
            count=npu.get('pe_count');id=self.node+':npu0'
            snap.devices.append(Device(id=id,kind='npu',name='Furiosa RNGD',node=self.node,metrics={'pe_count':metric(count,'PE','npu_driver','npu')}))
            snap.partitions.append(Partition(id=id,device_id=id,kind='pe',name='PE resource map',pe_indices=list(range(count)) if isinstance(count,int) else []))
            for process in npu['processes']:
                owner=pid_owner(process['pid'],pods,self.proc_root)
                match=next((w for w in snap.workloads if owner and (w.pod_uid,w.container)==owner),None)
                if match is None:
                    match=Workload(id=f'{id}:process:{process["pid"]}',name=f'PID {process["pid"]}',partition_id=id,origin='external process / unmatched Pod',state='observed');snap.workloads.append(match)
                match.observed['occupied_pe']=metric(','.join(map(str,process['pe_indices'])),'PE','npu_driver','npu')
                match.observed['host_pid']=metric(process['pid'],'PID','npu_driver','npu')
        return snap

def main():
    p=argparse.ArgumentParser();p.add_argument('--node',default=os.environ.get('NODE_NAME',os.uname().nodename));p.add_argument('--ingest',default=os.environ.get('KETRIS_INGEST_URL','http://ketris-dashboard-ingest.ketris.svc:8081/internal/v1/snapshots'))
    p.add_argument('--token-file',default='/var/run/ketris-auth/token');a=p.parse_args()
    token=Path(a.token_file).read_text().strip();providers=ReadOnlySources(a.node);stop=threading.Event()
    sources={name:SourceCache(name,interval,getattr(providers,name)) for name,interval in [('gpu',5),('npu',5),('npu_gates',2),('runtime',2),('checkpoint',2),('controller',2),('kubernetes',30)]}
    def source_loop(c):
        while not stop.is_set():c.poll();stop.wait(c.interval)
    for c in sources.values():threading.Thread(target=source_loop,args=(c,),daemon=True).start()
    threading.Thread(target=providers.watch_pods,args=(sources['kubernetes'],stop),daemon=True).start()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:stop.set())
    observer=NodeObserver(a.node,sources);session=uuid.uuid4().hex;sequence=0
    with httpx.Client(timeout=2) as client:
        while not stop.is_set():
            snapshot=observer.collect_node();payload={'session':session,'sequence':sequence,'snapshot':snapshot.model_dump()};sequence+=1
            try:
                r=client.post(a.ingest,json=payload,headers={'Authorization':'Bearer '+token});r.raise_for_status()
            except Exception as e:logging.warning('ingest unavailable: %s',type(e).__name__)
            stop.wait(2)
if __name__=='__main__':main()
