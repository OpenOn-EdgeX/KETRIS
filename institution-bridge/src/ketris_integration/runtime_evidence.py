"""Read regular JSON exports only. Never connects to native control sockets."""
from pathlib import Path
from datetime import datetime,timezone
import os,stat
from .contracts import *
def instant(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
def fresh(part,now):return 0<=(now-instant(part['observed_at'])).total_seconds()<=5
class ExportReader:
 def __init__(self,bindings_dir,partitions_dir):self.bindings_dir,self.partitions_dir=Path(bindings_dir),Path(partitions_dir)
 @staticmethod
 def _read(path):
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  try:
   info=os.fstat(fd)
   if not stat.S_ISREG(info.st_mode) or info.st_size>4*1024*1024:raise ValueError('not bounded regular JSON')
   with os.fdopen(fd,'r',closefd=False) as stream:data=stream.read(4*1024*1024+1)
   if len(data)>4*1024*1024:raise ValueError('oversize JSON')
   value=json.loads(data)
   if not isinstance(value,dict) or value.get('schema_version')!=1:raise ValueError('export schema mismatch')
   return value
  finally:os.close(fd)
 def _partition(self,target):return self._read(self.partitions_dir/(target.resource_name.split('/')[-1]+'.json'))
 def observe_target(self,target,now)->TargetObservation:
  try:
   p=self._partition(target)
   require(fresh(p,now() if callable(now) else now),'stale partition export')
   require(p['mig_uuid']==target.mig_uuid and p['node']==target.node,'target mismatch')
   return TargetObservation(True,p['generation'],inventory_json=canonical(p['inventory']),manager_state=p['manager_state'],observed_at=p['observed_at'])
  except (OSError,ValueError,KeyError,TypeError) as exc:return TargetObservation(False,reason=str(exc))
 def read_domain(self,target,pod_refs,now)->DomainSnapshot:
  p=self._partition(target);require(fresh(p,now() if callable(now) else now),'stale partition export');bindings=[]
  for t in p.get('tenants',[]):
   aid=t['allocation_id'];require(bool(re.fullmatch(r'a-[0-9a-f]+',aid)),'invalid allocation filename')
   try:bindings.append(self._read(self.bindings_dir/(aid+'.json')))
   except (OSError,ValueError):continue
  return DomainSnapshot(canonical(p),canonical(bindings),p['observed_at'])
def assess_apply(record,job,pod,domain,previous_sample,now)->ApplyAssessment:
 try:
  p=domain.partition;q=record.plan;tgt=q.target
  require(fresh(p,now),'stale partition export')
  require(p['manager_state']=='running' and p['generation']==tgt.inventory_generation and p['mig_uuid']==tgt.mig_uuid and p['gpu_uuid']==tgt.gpu_uuid and p['node']==tgt.node,'target not current/running')
  require(job['metadata']['uid']==record.job_uid and any(o.get('uid')==record.job_uid and o.get('kind')=='Job' for o in pod['metadata'].get('ownerReferences',[])),'Job/Pod owner UID mismatch')
  require(pod['spec'].get('nodeName')==tgt.node,'Pod node mismatch')
  ann=pod['metadata'].get('annotations',{})
  require(ann.get('ketris.integration/config-digest')==record.digest,'Pod approved config digest mismatch')
  status=next(x for x in pod['status']['containerStatuses'] if x['name']=='workload')
  require(status.get('restartCount',0)==0 and 'running' in status['state'],'container not running without restart')
  b=next(x for x in domain.bindings if x.get('pod_uid')==pod['metadata']['uid'] and x.get('container')=='workload')
  t=next(x for x in p['tenants'] if x['allocation_id']==b['allocation_id'])
  require(b['phase']=='bound' and t['phase']=='armed','binding/tenant not active')
  for key,val in dict(node=tgt.node,gpu_uuid=tgt.gpu_uuid,mig_uuid=tgt.mig_uuid,generation=tgt.inventory_generation,resource_name=tgt.resource_name,space_percent=q.space_percent,time_weight=q.time_weight,pod_uid=pod['metadata']['uid'],container_id=status['containerID'],actual_cuda_uuid=tgt.mig_uuid).items():
   require(b.get(key)==val and t.get(key)==val,'native identity/policy mismatch: '+key)
  generation=b['runtime_generation'];require(generation and t['runtime_generation']==generation,'runtime generation mismatch')
  require(instant(status['state']['running']['startedAt']).timestamp()*1000000<=b['runtime_observed_at_us'],'runtime predates container')
  require(b['reported_sm_count']==t['reported_sm_count'] and 0<b['reported_sm_count']<=p['inventory']['profile_sm'],'invalid reported SM')
  peers=[x for x in p['tenants'] if x.get('phase')=='armed'];total_weight=sum(x['time_weight'] for x in peers)
  require(abs(t['budget_us']-round(10000*q.time_weight/total_weight))<=1,'active peer budget mismatch')
  sample=dict(at=p['observed_at'],runtime_generation=generation,pod_uid=b['pod_uid'],container_id=b['container_id'],allocation_id=b['allocation_id'],charged_total_us=t['charged_total_us'],kernels=t['kernels'])
  require(type(sample['kernels']) is int and sample['kernels']>=0 and type(sample['charged_total_us']) is int and sample['charged_total_us']>=0,'invalid counters')
  ev=dict(approved_config_digest=record.digest,config_version='cfg-'+record.digest[:16],job_uid=record.job_uid,pod_uid=b['pod_uid'],container_id=b['container_id'],allocation_id=b['allocation_id'],runtime_generation=generation,actual_cuda_uuid=b['actual_cuda_uuid'],reported_sm_count=b['reported_sm_count'],node=tgt.node,gpu_uuid=tgt.gpu_uuid,mig_uuid=tgt.mig_uuid,resource_name=tgt.resource_name,inventory_generation=tgt.inventory_generation,space_percent=q.space_percent,time_weight=q.time_weight,budget_us=t['budget_us'],period_us=10000,active_peer_weights=[x['time_weight'] for x in peers],sample=sample)
  old=json.loads(previous_sample or '{}') if isinstance(previous_sample,(str,type(None))) else previous_sample
  same=old and all(old.get(k)==sample[k] for k in ('runtime_generation','pod_uid','container_id','allocation_id'))
  if same and 0<(instant(sample['at'])-instant(old['at'])).total_seconds()<=30 and sample['kernels']>old['kernels'] and sample['charged_total_us']>old['charged_total_us']:
   ev.update(start=old,end=sample,kernels_delta=sample['kernels']-old['kernels'],charged_delta_us=sample['charged_total_us']-old['charged_total_us']);state='EXECUTION_OBSERVED'
  else:state='APPLIED_VERIFIED'
  ev['evidence_digest']=sha(canonical(ev))
  return ApplyAssessment(state,evidence_json=canonical(ev),sample_json=canonical(sample))
 except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:return ApplyAssessment('ADMITTED','native evidence unavailable: '+str(exc),record.evidence_json,record.sample_json)
