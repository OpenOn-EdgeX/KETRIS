"""Join kubelet's actual device allocation to Pod UID and CUDA runtime identity."""
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
from .contracts import utc_now
from .state import read_json

def device_ids(entry):
    ids=entry.get('DeviceIDs',[])
    return sorted(v for values in ids.values() for v in values) if isinstance(ids,dict) else sorted(ids)

def runtime_info(binding):
    info=read_json(Path(binding.control_dir)/'device.json')
    pid=info.get('pid')
    if type(pid) is not int or pid<1: raise ValueError('invalid runtime PID')
    socket=Path(binding.control_dir)/f'bless-{pid}.sock'
    if not socket.exists(): raise ValueError('runtime socket missing')
    return info,str(socket)

def reconcile_bindings(allocations,checkpoint,pods,inventory,state_root,prior):
    root=Path(state_root).resolve();entries={(e.mig_uuid,e.generation):e for e in inventory}
    pod_by_uid={p.get('metadata',{}).get('uid'):p for p in pods}
    cp=checkpoint.get('Data',{}).get('PodDeviceEntries') or [];result=[]
    for b in allocations:
        if Path(b.control_dir).resolve()!=root/'control'/b.allocation_id:
            raise ValueError('allocation control directory outside managed root')
        old=prior.get(b.allocation_id,{})
        bound=replace(b,updated_at=utc_now())
        matches=[x for x in cp if x.get('ResourceName')==b.resource_name and device_ids(x)==sorted(b.device_ids)]
        if len(matches)>1:
            bound.phase='failed';result.append(bound);continue
        if not matches:
            if old.get('pod_uid'):
                pod=pod_by_uid.get(old['pod_uid'])
                statuses=(pod or {}).get('status',{}).get('containerStatuses',[])
                running=any(s.get('name')==old.get('container') and 'running' in s.get('state',{}) for s in statuses)
                if not running: bound.phase='completed'
            result.append(bound);continue
        x=matches[0];uid=x['PodUID'];name=x['ContainerName'];pod=pod_by_uid.get(uid)
        if old.get('pod_uid') and old['pod_uid']!=uid:
            bound.phase='failed';result.append(bound);continue
        # Track the owner even before CUDA initialization; this does not arm it.
        bound=replace(bound,pod_uid=uid,container=name)
        if not pod or (b.mig_uuid,b.generation) not in entries:
            result.append(bound);continue
        statuses=pod.get('status',{}).get('containerStatuses',[])
        status=next((s for s in statuses if s.get('name')==name),{})
        c=next((c for c in pod.get('spec',{}).get('containers',[]) if c.get('name')==name),{})
        if 'running' not in status.get('state',{}):
            result.append(bound);continue
        try:
            weight=int(pod.get('metadata',{}).get('annotations',{}).get('ketris.re.kr/time-weight','1'))
            env={e.get('name'):e.get('value') for e in c.get('env',[])}
            if env.get('KETRIS_TIME_WEIGHT')!=str(weight): raise ValueError('time contract changed')
            if int(c.get('resources',{}).get('limits',{}).get(b.resource_name,0))!=b.space_percent:
                raise ValueError('space contract changed')
            info,sock=runtime_info(b)
            entry=entries[(b.mig_uuid,b.generation)]
            if info.get('actual_cuda_uuid')!=b.mig_uuid or info.get('device_count')!=1 or info.get('cuda_error')!=0 or info.get('sm_error')!=0 or info.get('start_gated') is not True:
                raise ValueError('actual CUDA device is not the allocated MIG')
            sm=info.get('reported_sm_count')
            if type(sm) is not int or sm<1 or (entry.profile_sm and sm>entry.profile_sm): raise ValueError('invalid reported SM count')
            started=datetime.fromisoformat(status['state']['running']['startedAt'].replace('Z','+00:00')).timestamp()*1e6
            if info.get('observed_at_us',0)<started or not info.get('runtime_generation'): raise ValueError('stale runtime generation')
            bound=replace(bound,pod_uid=uid,container=name,container_id=status.get('containerID'),time_weight=weight,phase='bound')
            # Validation after assigning optional fields prevents out-of-range weights.
            bound.__post_init__()
        except FileNotFoundError:
            pass # allocated Pod has not initialized CUDA yet
        except (ValueError,KeyError,TypeError):
            bound.phase='failed'
        result.append(bound)
    return result

