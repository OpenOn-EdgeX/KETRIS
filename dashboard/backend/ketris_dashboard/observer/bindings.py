import re
import math
from ..models import Workload,observed

def checkpoint_workloads(node,checkpoint,pods):
    by_uid={p['metadata']['uid']:p for p in pods};out=[]
    for entry in checkpoint.get('Data',{}).get('PodDeviceEntries') or []:
        uid=entry.get('PodUID');container=entry.get('ContainerName');pod=by_uid.get(uid)
        if not pod:continue
        c=next((c for c in pod['spec']['containers'] if c['name']==container),None)
        if c is None:continue
        resource=entry.get('ResourceName','')
        if resource.startswith('keti.re.kr/mig-'):continue # verified runtime join is separate
        if resource not in ['keti.re.kr/npu-pe','keti.re.kr/lsu']:continue
        ids=entry.get('DeviceIDs') or {};ids=[v for values in ids.values() for v in values] if isinstance(ids,dict) else ids
        is_npu=resource=='keti.re.kr/npu-pe'
        matches=[re.search(r'(?:^|-)(?:rngd|npu)\d+-pe(\d+)$',id) for id in ids] if is_npu else []
        pe=[int(m[1]) for m in matches] if matches and all(matches) else []
        requested=c.get('resources',{}).get('limits',{}).get(resource)
        try:requested=float(requested)
        except (ValueError,TypeError):requested=None
        group={'pe_count' if is_npu else 'lsu':observed(requested,'PE' if is_npu else 'LSU','kubernetes',quality='configured')}
        out.append(Workload(id=f'{node}:{uid}:{container}',name=pod['metadata']['name'],pod_uid=uid,container=container,namespace=pod['metadata'].get('namespace'),
                            partition_id=node+':npu0' if is_npu else None,requested=group,applied={'pe_indices':observed(','.join(map(str,pe)) if pe else None,'PE','allocation')},state=pod.get('status',{}).get('phase','unavailable')))
    return out

def pid_owner(pid,pods,proc_root):
    try:text=(proc_root/str(pid)/'cgroup').read_text()
    except OSError:return None
    for pod in pods:
        uid=pod['metadata']['uid']
        if not re.search('pod'+re.escape(uid.replace('-','_'))+r'(?:\.slice|/)',text) and not re.search('pod'+re.escape(uid)+r'(?:\.slice|/)',text):continue
        for status in pod.get('status',{}).get('containerStatuses',[]):
            cid=status.get('containerID','').split('://')[-1]
            if cid and cid in text and 'running' in status.get('state',{}):return uid,status['name']
    return None

def pending_workloads(node,pods,known,migs):
    """Pod phase/requests are not proof that CUDA or a PE assignment was applied."""
    existing={(w.pod_uid,w.container) for w in known};by_resource={m['resource_name']:m for m in migs if 'resource_name' in m};out=[]
    for pod in pods:
        uid=pod['metadata']['uid']
        if pod.get('status',{}).get('phase') not in ['Pending','Running']:continue
        for c in pod['spec']['containers']:
            if (uid,c['name']) in existing:continue
            for key,value in c.get('resources',{}).get('limits',{}).items():
                kind='mig' if key.startswith('keti.re.kr/mig-') else 'pe' if key=='keti.re.kr/npu-pe' else 'lsu' if key=='keti.re.kr/lsu' else None
                if kind is None:continue
                try:quantity=float(value);quantity=quantity if math.isfinite(quantity) and quantity>0 else None
                except (TypeError,ValueError):quantity=None
                try:weight=int(pod['metadata'].get('annotations',{}).get('ketris.re.kr/time-weight','1'))
                except ValueError:weight=None
                entry=by_resource.get(key);pid=node+':'+entry['mig_uuid'] if entry else node+':npu0' if kind=='pe' else None
                out.append(Workload(id=f'{node}:{uid}:{c["name"]}',name=pod['metadata']['name'],partition_id=pid,pod_uid=uid,container=c['name'],namespace=pod['metadata'].get('namespace'),state=pod['status']['phase'],
                    requested={('space_percent' if kind=='mig' else 'pe_count' if kind=='pe' else 'lsu'):observed(quantity,'%' if kind=='mig' else 'PE' if kind=='pe' else 'LSU','kubernetes',quality='configured'),
                               'time_weight':observed(weight if kind=='mig' else None,'weight','kubernetes',quality='configured')},
                    applied={'mps_percent':observed(None,'%','allocation'),'actual_cuda_uuid':observed(None,None,'runtime')},
                    observed={'charged_share':observed(None,'fraction','runtime',reason='requested container has no verified execution binding')}))
    return out
