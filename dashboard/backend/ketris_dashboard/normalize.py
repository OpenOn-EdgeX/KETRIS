from dataclasses import dataclass
import math,re
from .models import ViewSnapshotV1,Device,Partition,Workload,SourceStatus,observed

@dataclass(frozen=True)
class CounterPoint:
    value:float|None
    at:float
    generation:str
    domain:str

@dataclass(frozen=True)
class DeltaResult:
    valid:bool
    delta:float|None
    reason:str|None
    domain:str
    start:float|None
    end:float

@dataclass(frozen=True)
class ShareResult:
    completeness:str
    subjects:list[str]
    shares:dict[str,float|None]

def counter_delta(previous,current):
    reason=None
    if previous is None:reason='first sample'
    elif (previous.generation,previous.domain)!=(current.generation,current.domain):reason='identity changed'
    elif current.at-previous.at<=0 or current.at-previous.at>30:reason='unobserved interval'
    elif any(v is None or isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in [previous.value,current.value]):reason='missing/nonfinite sample'
    elif current.value<previous.value:reason='counter reset'
    return DeltaResult(reason is None,current.value-previous.value if reason is None else None,reason,current.domain,previous.at if previous else None,current.at)

def partition_shares(streams):
    domains={}
    for key,delta in streams.items():domains.setdefault(delta.domain,{})[key]=delta
    out={}
    for domain,values in domains.items():
        valid=all(d.valid for d in values.values()) and len({(d.start,d.end) for d in values.values()})==1
        total=sum(d.delta for d in values.values()) if valid else 0
        valid=valid and total>0
        out[domain]=ShareResult('complete' if valid else 'partial',list(values),{k:d.delta/total if valid else None for k,d in values.items()})
    return out

def normalize_runtime(state):
    pattern=r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    if state.get('schema_version')!=1 or not re.fullmatch('GPU-'+pattern,state.get('gpu_uuid','')) or not re.fullmatch('MIG-'+pattern,state.get('mig_uuid','')):
        raise ValueError('actual parent/MIG identity required')
    node=state['node'];at=state['observed_at'];device_id=node+':'+state['gpu_uuid'];part_id=node+':'+state['mig_uuid'];inv=state['inventory']
    d=Device(id=device_id,kind='gpu',name=inv.get('gpu_name','GPU'),node=node)
    p=Partition(id=part_id,device_id=device_id,kind='mig',name=inv.get('profile') or 'MIG',generation=state['generation'],metrics={
        'mig_uuid':observed(state['mig_uuid'],None,'gpu_driver',at),'profile_sm':observed(inv.get('profile_sm'),'SM','gpu_driver',at),
        'memory_mib':observed(inv.get('memory_mib'),'MiB','gpu_driver',at)})
    workloads=[]
    for t in state['tenants']:
        uid=t.get('pod_uid');container=t.get('container');id=f'{node}:{uid}:{container}:{t["allocation_id"]}'
        metric=lambda value,unit,quality='observed',reason=None:observed(value,unit,'runtime',at,quality,reason)
        w=Workload(id=id,name=t.get('pod_name') or t['allocation_id'],partition_id=part_id,pod_uid=uid,container=container,namespace=t.get('namespace'),state=t.get('phase','unavailable'),requested={
            'space_percent':observed(t.get('space_percent'),'%','allocation',at,'configured'),'time_weight':observed(t.get('time_weight'),'weight','allocation',at,'configured')},applied={
            'mps_percent':metric(t.get('space_percent'),'%','configured'),'reported_sm_count':metric(t.get('reported_sm_count'),'SM'),
            'actual_cuda_uuid':metric(t.get('actual_cuda_uuid'),None),'time_share':metric(t.get('configured_share'),'fraction','configured'),
            'budget_us':metric(t.get('budget_us'),'us/10ms','configured')},observed={
            'charged_share':metric(t.get('observed_share'),'fraction','derived','charged kernel wall time within this MIG observation window'),
            'charged_total_us':metric(t.get('charged_total_us'),'us'),'kernels':metric(t.get('kernels'),'kernels'),'credit_us':metric(t.get('credit_us'),'us')})
        workloads.append(w)
    status='ok' if state['manager_state']=='running' else 'unavailable'
    return ViewSnapshotV1(node=node,captured_at=at,devices=[d],partitions=[p],workloads=workloads,source_status={'runtime':SourceStatus(status=status,last_success=at if status=='ok' else None)})
