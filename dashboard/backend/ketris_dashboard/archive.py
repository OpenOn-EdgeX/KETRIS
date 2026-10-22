"""Pure allowlisted report adapters. Only measured windows become frames."""
from dataclasses import dataclass,asdict
import hashlib,json,os,re,math
from pathlib import Path
from .models import Device,Partition,Workload,ViewSnapshotV1,ReplayFrame,SourceStatus,observed

REPORTS=Path('/root/workspace/prism-server-task/reports')
DEFAULT_ROOTS=[REPORTS/n for n in ['ketris_gpu_repro_20261012','ketris_mig_repro_20261012','ketris_rngd_repro_20261012']]

@dataclass
class ExperimentBundle:
    experiment_id:str
    source_manifest:list[dict]
    metadata:dict
    summary:dict
    frames:list[ReplayFrame]
    evidence:dict
    def to_dict(self):
        return {'experiment_id':self.experiment_id,'source_manifest':self.source_manifest,'metadata':self.metadata,'summary':self.summary,
                'frames':[f.model_dump() for f in self.frames],'evidence':self.evidence}

class Reader:
    def __init__(self,root):self.root=root;self.manifest=[];self.evidence={}
    def read(self,path):
        path=path.resolve()
        if not path.is_relative_to(self.root) or path.stat().st_size>2*1024*1024:raise ValueError('archive path escape or oversized source')
        raw=path.read_bytes();relative=str(path.relative_to(self.root));digest=hashlib.sha256(raw).hexdigest()
        self.manifest.append({'path':relative,'sha256':digest,'bytes':len(raw)})
        return json.loads(raw),digest
    def add_evidence(self,digest,path,projection):
        id='e-'+digest[:24]
        self.evidence[id]={'id':id,'source_path':str(path.relative_to(self.root)),'sha256':digest,'projection':projection}
        return id

def metric(value,unit,quality='observed',reason=None):return observed(value,unit,'archive',None,quality,reason)

def _gpu_frame(row,idx,mig,partition):
    config=row['config'];repeat=row['repeat'];node='gpu-npu-server-02 (archive)'
    gpu=partition.get('gpu_uuid') if mig else None
    device_id='archive:'+(gpu or 'host-gpu');part_id='archive:'+(partition.get('mig_uuid') if mig else 'full-gpu')
    device=Device(id=device_id,kind='gpu',name='RTX PRO 6000 Blackwell',node=node,metrics={'gpu_uuid':metric(gpu,None,'observed')})
    part=Partition(id=part_id,device_id=device_id,kind='mig' if mig else 'gpu',name=partition.get('profile','Full GPU'),historical=True,
        metrics={'mig_uuid':metric(partition.get('mig_uuid'),None,'configured' if mig else 'unavailable'),
                 'profile_sm':metric(partition.get('partition_sm',188 if not mig else None),'SM')})
    workers=row['workers'];weights=row.get('time_weights');spaces=row.get('space_pct',[])
    def delta(w):
        raw=w.get('runtime_delta') or w
        return {k:v if type(v) in (int,float) and math.isfinite(v) and v>=0 else None
                for k in ['charged_us','kernels'] for v in [raw.get(k)]}
    charged=[delta(w).get('charged_us') for w in workers.values()]
    valid=all(v is not None and v>=0 for v in charged) and sum(charged)>0
    workloads=[];projection={}
    for i,(name,w) in enumerate(workers.items()):
        amounts=delta(w);share=amounts.get('charged_us',0)/sum(charged) if valid else None
        ips=w.get('iters_per_second',w.get('iterations_per_second'))
        space=spaces[i] if i<len(spaces) else int(w['mps_pct']) if w.get('mps_pct') else None
        target=weights[i]/sum(weights) if weights else None
        origin='MOCK 기관 요청 · K8s 실험' if mig else 'host process'
        workloads.append(Workload(id=f'archive:{config}:{name}',name=name,partition_id=part_id,pod_uid=w.get('pod_uid'),origin=origin,state='measured',
            requested={'space_percent':metric(space,'%','configured'),'time_weight':metric(weights[i] if weights else None,'weight','configured')},
            applied={'mps_percent':metric(space,'%','configured'),'reported_sm_count':metric(w.get('reported_sm_count'),'SM'),
                     'selected_mig_uuid':metric(w.get('mig_uuid'),None,'configured'),
                     'actual_cuda_uuid':metric(w.get('actual_cuda_uuid'),None,'observed',None if w.get('actual_cuda_uuid') else 'old report only recorded CUDA_VISIBLE_DEVICES'),
                     'time_share':metric(target,'fraction','configured')},
            observed={'charged_share':metric(share,'fraction','derived','sum of complete same-window kernel wall-time deltas'),
                      'charged_us':metric(amounts.get('charged_us'),'us'),'kernels':metric(amounts.get('kernels'),'kernels'),
                      'throughput':metric(ips,'iter/s'),'iterations':metric(w.get('iterations'),'iterations')}))
        projection[name]={'reported_sm_count':w.get('reported_sm_count'),'selected_mig_uuid':w.get('mig_uuid'),'actual_cuda_uuid':w.get('actual_cuda_uuid'),
                          'mps_percent':space,'iterations':w.get('iterations'),'elapsed_s':w.get('elapsed_s'),'iterations_per_second':ips,'runtime_delta':amounts}
    snapshot=ViewSnapshotV1(node=node,mode='replay',captured_at=None,devices=[device],partitions=[part],workloads=workloads,
        source_status={'archive':SourceStatus(status='ok',reason='measured windows; original wall-clock timestamp unavailable')})
    duration=max(w.get('elapsed_s',20) for w in workers.values())
    return ReplayFrame(index=idx,interval={'condition':config,'repeat':repeat,'start':(repeat-1)*20,'end':(repeat-1)*20+duration},axis_kind='measurement_window',snapshot=snapshot),projection

def _npu_frame(row,idx):
    config=row['config'];repeat=row['repeat'];node='gpu-npu-server-02 (archive)';dev='archive:npu0'
    device=Device(id=dev,kind='npu',name='Furiosa RNGD · 8 PE',node=node,metrics={'pe_count':metric(8,'PE')})
    partitions=[];workloads=[];projection={}
    for bi,(port,block) in enumerate(sorted(row['blocks'].items())):
        pe=list(range(8)) if config=='time_only_70_30' else list(range(bi*4,bi*4+4));pid='archive:npu0:'+port
        partitions.append(Partition(id=pid,device_id=dev,kind='pe',name=f'{len(pe)} PE block · {port}',pe_indices=pe,historical=True))
        before=row['stats_before'].get(port,{});after=row['stats_after'].get(port,{})
        deltas={n:{k:after[n].get(k,0)-before.get(n,{}).get(k,0) for k in ['charged_us','charged_extra_us','done']} for n in block['tenants'] if n in after and n in before}
        complete=len(deltas)==len(block['tenants']) and all(v['charged_us']>=0 for v in deltas.values())
        denom=sum(v['charged_us'] for v in deltas.values()) if complete else 0
        for i,(name,t) in enumerate(block['tenants'].items()):
            delta=deltas.get(name,{});share=delta.get('charged_us',0)/denom if denom>0 else None
            target=(.7 if i==0 else .3) if config!='space_only' else None
            workloads.append(Workload(id=f'archive:{config}:{port}:{name}',name=name,partition_id=pid,origin='K8s RNGD 실험',state='measured',requested={
                'pe_count':metric(len(pe),'PE','configured'),'time_ratio':metric(target,'fraction','configured')},applied={'time_share':metric(target,'fraction','configured')},observed={
                'charged_share':metric(share,'fraction','derived','contract generate-call wall time; same PE block'),
                'charged_us':metric(delta.get('charged_us'),'us'),'charged_extra_us':metric(delta.get('charged_extra_us'),'us'),
                'throughput':metric(t.get('rps_drain_inclusive'),'req/s'),'requests_done':metric(t.get('done'),'requests'),
                'p95_completion_ms':metric(t.get('p95_completion_ms'),'ms'),'p50_completion_ms':metric(t.get('p50_completion_ms'),'ms')}))
        # Only numeric gate/aggregate metrics. Request bodies and inference text never enter evidence.
        projection[port]={'elapsed_s':block.get('elapsed_s'),'offered_duration_s':block.get('offered_duration_s'),
                          'tenants':{n:{k:t.get(k) for k in ['done','rps_drain_inclusive','p50_completion_ms','p95_completion_ms']} for n,t in block['tenants'].items()},'deltas':deltas}
    snap=ViewSnapshotV1(node=node,mode='replay',captured_at=None,devices=[device],partitions=partitions,workloads=workloads,
        source_status={'archive':SourceStatus(status='ok',reason='completion latency; not true TTFT')})
    duration=max(b.get('elapsed_s',60) for b in row['blocks'].values())
    return ReplayFrame(index=idx,interval={'condition':config,'repeat':repeat,'start':(repeat-1)*60,'end':(repeat-1)*60+duration},axis_kind='measurement_window',snapshot=snap),projection

def _safe_summary(value):
    allowed={'solo100','solo25','solo50','solo75','space50_50','space75_25','time1_1','time3_1','combined50_50_time3_1','space_only','spacetime_70_30','time_only_70_30',
        'A','B','C','D','workers','tenants','repeats','reported_sm','worker_ips','total_ips_mean','total_ips_sd','charged_share_a_mean','charged_share_a_sd',
        'space_pct','reported_sm_count','target_time_share','mean_iters_per_second','stdev_iters_per_second','mean_charged_share','time_share_error_pp',
        'total_rps','mean_total_rps','stdev_total_rps','completion_share','contract_time_share','total_accounted_time_share','p95_completion_ms'}
    if isinstance(value,dict):return {k:_safe_summary(v) for k,v in value.items() if k in allowed}
    if isinstance(value,list):return [_safe_summary(v) for v in value]
    if value is None or isinstance(value,(int,float,str,bool)):return value
    raise ValueError('unsupported summary value')

def parse_experiment(path:Path,allowed_roots=None)->ExperimentBundle:
    root=Path(path).resolve()
    roots=allowed_roots or DEFAULT_ROOTS+[Path(p) for p in os.environ.get('KETRIS_ARCHIVE_ROOTS','').split(os.pathsep) if p]
    if root not in [Path(p).resolve() for p in roots] or not root.is_dir():raise ValueError('report directory is not allowlisted')
    r=Reader(root);raw_summary,_=r.read(root/'summary.json');summary=_safe_summary(raw_summary.get('summary',{}))
    npu='rngd' in root.name;mig='mig' in root.name
    partition={}
    if mig:
        if (root/'partition.json').exists():partition,_=r.read(root/'partition.json')
        else:partition=raw_summary.get('partition',{})
    files=sorted(root.glob('*-[1-3].json')) if npu else sorted(root.glob('*/measurement-[1-3].json'))
    frames=[]
    for file in files:
        row,digest=r.read(file)
        if row.get('warmup'):continue
        if mig and (file.parent/'input.json').exists():
            contract,_=r.read(file.parent/'input.json')
            if 'space_percent' in contract:
                row['space_pct']=contract['space_percent']
                row['time_weights']=contract['time_weights'] if contract.get('time_policy')!='unlimited_calibration' else None
        frame,projection=_npu_frame(row,len(frames)) if npu else _gpu_frame(row,len(frames),mig,partition)
        id=r.add_evidence(digest,file,{'config':row['config'],'repeat':row['repeat'],'workers' if not npu else 'blocks':projection})
        frame.evidence_ids=[id];frames.append(frame)
    r.manifest.sort(key=lambda x:x['path'])
    digest=hashlib.sha256(json.dumps(r.manifest,sort_keys=True).encode()).hexdigest()[:16]
    metadata={'name':root.name,'kind':'rngd' if npu else 'mig' if mig else 'gpu','axis_kind':'measurement_window',
              'origin':'MOCK 기관 요청' if mig else 'K8s RNGD 실험' if npu else 'host process','historical':True,
              'precision_verdict':'not_reproduced: RNGD 70:30 contract target' if npu else 'reported measurements',
              'limitations':['charged wall time is not exclusive device busy time','MPS limits are shared caps, not dedicated SM ownership']}
    return ExperimentBundle(root.name+'-'+digest,r.manifest,metadata,summary,frames,r.evidence)
