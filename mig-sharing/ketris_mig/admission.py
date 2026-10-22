"""Read-only request validation. Device selection belongs to the MIG resource."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import urllib.request
from .contracts import ShareRequest, device_uuid
from .inventory import MigInventoryEntry

PREFIX='keti.re.kr/mig-'
ANNOTATION='ketris.re.kr/'
CANONICAL={'BLESS_TIME_START_GATED':'1','BLESS_TIME_WC':'0','BLESS_GATE_METRICS':'1'}
PROTECTED={'CUDA_VISIBLE_DEVICES','NVIDIA_VISIBLE_DEVICES','CUDA_MPS_PIPE_DIRECTORY',
           'CUDA_MPS_ACTIVE_THREAD_PERCENTAGE','LD_PRELOAD','LD_LIBRARY_PATH',
           'KETRIS_ALLOCATION_ID','KETRIS_DEVICE_INFO_LOG'}


def _keys(c):
    res=c.get('resources') or {}
    return set((res.get('limits') or {})) | set((res.get('requests') or {}))


def has_mig_request(pod):
    spec=pod.get('spec') or {}
    return any(any(k.startswith(PREFIX) for k in _keys(c))
               for c in (spec.get('containers') or [])+(spec.get('initContainers') or []))


def _quantity(v):
    if isinstance(v,str) and re.fullmatch(r'[0-9]+',v): v=int(v)
    if type(v) is not int or not 1<=v<=100: raise ValueError('MIG space must be 1..100 integer units')
    return v


@dataclass
class ValidationResult:
    contracts: dict[str,ShareRequest]=field(default_factory=dict)
    patches: list[dict]=field(default_factory=list)


def validate_pod(pod: dict, inventory: list[MigInventoryEntry]) -> ValidationResult:
    result=ValidationResult()
    if not has_mig_request(pod): return result
    spec=pod.get('spec') or {}
    if any(any(k.startswith(PREFIX) for k in _keys(c)) for c in spec.get('initContainers') or []):
        raise ValueError('MIG sharing init containers are not supported')
    ann=(pod.get('metadata') or {}).get('annotations') or {}
    raw_weight=ann.get(ANNOTATION+'time-weight','1')
    if not isinstance(raw_weight,str) or not re.fullmatch(r'[1-9][0-9]{0,3}',raw_weight):
        raise ValueError('time weight must be an integer string in 1..1000')
    weight=int(raw_weight)
    by_name={x.resource_name:x for x in inventory}
    if len(by_name)!=len(inventory): raise ValueError('ambiguous MIG resource catalog')
    for i,c in enumerate(spec.get('containers') or []):
        keys=_keys(c);selected=[k for k in keys if k.startswith(PREFIX)]
        if not selected: continue
        if c.get('envFrom'): raise ValueError('MIG containers require explicit env names; envFrom cannot be verified without reading secrets')
        if len(selected)!=1 or any(k.startswith(('keti.re.kr/lsu','nvidia.com/')) for k in keys):
            raise ValueError('a container must request exactly one MIG share resource')
        key=selected[0]
        if key not in by_name: raise ValueError('requested MIG is absent, stale, or unsupported')
        entry=by_name[key];res=c.get('resources') or {}
        space=_quantity((res.get('limits') or {}).get(key))
        if _quantity((res.get('requests') or {}).get(key))!=space:
            raise ValueError('MIG share requests and limits must match')
        if ANNOTATION+'mig-uuid' in ann and device_uuid(ann[ANNOTATION+'mig-uuid'])!=entry.mig_uuid:
            raise ValueError('annotation UUID differs from the allocated MIG resource')
        request=ShareRequest(entry.mig_uuid,space,weight)
        name=c.get('name')
        if not name or name in result.contracts: raise ValueError('container identity missing or duplicated')
        result.contracts[name]=request
        canonical={**CANONICAL,'KETRIS_TIME_WEIGHT':str(weight)}
        envs=c.get('env') or [];seen=set()
        for e in envs:
            k=e.get('name')
            if k in seen: raise ValueError('duplicate environment names')
            seen.add(k)
            if k in canonical:
                if e.get('value')!=canonical[k] or 'valueFrom' in e: raise ValueError('MIG policy env differs from declared contract')
            elif k in PROTECTED or str(k).startswith('BLESS_'):
                raise ValueError('MIG device/runtime environment is managed by Allocate')
        missing=[{'name':k,'value':v} for k,v in canonical.items() if k not in seen]
        base=f'/spec/containers/{i}/env'
        if c.get('env') is None: result.patches.append({'op':'add','path':base,'value':missing})
        else: result.patches.extend({'op':'add','path':base+'/-','value':e} for e in missing)
    return result


def load_admission_inventory() -> list[MigInventoryEntry]:
    path=os.environ.get('KETRIS_MIG_INVENTORY_FILE')
    if path: catalog=json.loads(Path(path).read_text())
    else:
        sa=Path('/var/run/secrets/kubernetes.io/serviceaccount')
        host=os.environ.get('KUBERNETES_SERVICE_HOST')
        if not host: raise ValueError('MIG catalog is unavailable')
        ns=os.environ.get('KETRIS_MIG_CATALOG_NAMESPACE','kube-system')
        url=f'https://{host}:{os.environ.get("KUBERNETES_SERVICE_PORT","443")}/api/v1/namespaces/{ns}/configmaps/ketris-mig-inventory'
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+(sa/'token').read_text().strip()})
        with urllib.request.urlopen(req,context=ssl.create_default_context(cafile=str(sa/'ca.crt')),timeout=2) as reply:
            catalog=json.loads(json.load(reply)['data']['catalog.json'])
    now=datetime.now(timezone.utc);entries=[]
    for node in catalog.get('nodes',{}).values():
        try:
            ts=datetime.fromisoformat(node['observed_at'].replace('Z','+00:00'))
            age=(now-ts).total_seconds()
        except (KeyError,ValueError,TypeError): continue
        if node.get('status')!='ok' or not -5<=age<=15: continue
        for item in node.get('instances',[]):
            value={k:v for k,v in item.items() if k!='resource_name'}
            entry=MigInventoryEntry(**value)
            if item.get('resource_name',entry.resource_name)!=entry.resource_name: raise ValueError('invalid MIG catalog resource identity')
            entries.append(entry)
    return entries

def mig_admission_manifest(legacy):
    """Reuse the installed certificate/service; fail closed only for MIG resources."""
    import copy
    wh=copy.deepcopy(legacy['webhooks'][0])
    wh.update(name='mig-contract.ketris.keti.re.kr',failurePolicy='Fail');wh.pop('namespaceSelector',None)
    wh['rules']=[{'operations':['CREATE','UPDATE'],'apiGroups':[''],'apiVersions':['v1'],'resources':['pods'],'scope':'Namespaced'}]
    check="has(c.resources) && ((has(c.resources.limits) && c.resources.limits.exists(k, k.startsWith('keti.re.kr/mig-'))) || (has(c.resources.requests) && c.resources.requests.exists(k, k.startsWith('keti.re.kr/mig-'))))"
    expression='object.spec.containers.exists(c, '+check+') || (has(object.spec.initContainers) && object.spec.initContainers.exists(c, '+check+'))'
    wh['matchConditions']=[{'name':'mig-resource','expression':expression}]
    return {'apiVersion':'admissionregistration.k8s.io/v1','kind':'MutatingWebhookConfiguration','metadata':{'name':'ketris-mig-admission'},'webhooks':[wh]}
