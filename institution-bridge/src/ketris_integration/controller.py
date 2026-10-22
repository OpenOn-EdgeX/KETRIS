"""Reconciliation performs bounded status writes; samples stay in SQLite."""
from dataclasses import replace
from datetime import datetime,timezone
import json,logging
from .contracts import *
from .store import TERMINAL
from .jobs import ensure_job
from .admission import evaluate_admission,own_check_patch,condition,CHECK,FLAVOR,verify_admitted_approval
from .runtime_evidence import assess_apply
log=logging.getLogger('ketris.integration')
class BridgeController:
 def __init__(self,store,kube,exports,settings,clock=None):self.store,self.kube,self.exports,self.settings=store,kube,exports,settings;self.clock=clock or (lambda:datetime.now(timezone.utc))
 def reconcile_once(self,now):
  workloads=self.kube.list_workloads()
  for r in self.store.list_executions():
   if r.state in TERMINAL or not r.profile or not r.plan or not r.profile.executable or not r.plan.executable:continue
   try:self._one(r,workloads,now)
   except ContractError as e:self.store.transition(r.workload_id,'REPLAN_REQUIRED',e.reason)
   except Exception as e:log.warning(canonical(dict(event='reconcile_error',workload_id=r.workload_id,error=type(e).__name__)))
 def _one(self,r,workloads,now):
  require(r.profile.profile_id==r.plan.profile_id and r.profile.version==r.plan.profile_version and r.profile.workload_id==r.plan.workload_id==r.workload_id and r.profile.trace_id==r.plan.trace_id,'exact profile reference unresolved')
  target=self.exports.observe_target(r.plan.target,self.clock)
  if not r.job_uid:
   if min(int(r.profile.expires_at_unix_s),int(r.plan.expires_at_unix_s))<=self.clock().timestamp():raise ContractError('FAILED_PRECONDITION','inputs expired before submission')
   if not target.available or target.generation!=r.plan.target.inventory_generation:raise ContractError('FAILED_PRECONDITION','live target unavailable or generation mismatch')
   ensure_job(r,self.store,self.kube,self.settings.image);r=self.store.get_execution(r.workload_id)
  job=self.kube.get_job(r.job_name)
  if not job or job['metadata']['uid']!=r.job_uid:raise ContractError('FAILED_PRECONDITION','recorded Job absent or UID changed')
  w=next((x for x in workloads if any(o.get('uid')==r.job_uid and o.get('kind')=='Job' for o in x['metadata'].get('ownerReferences',[]))),None)
  if not w:return
  pods=self.kube.list_pods(r.job_uid)
  if any(c.get('type')=='Failed' and c.get('status')=='True' for c in job.get('status',{}).get('conditions',[])):self.store.transition(r.workload_id,'FAILED','Job failed');return
  complete=any(c.get('type')=='Complete' and c.get('status')=='True' for c in job.get('status',{}).get('conditions',[]))
  if complete and condition(w,'Finished').get('status')=='True' and len(pods)==1:
   pod=pods[0];cs=next((x for x in pod.get('status',{}).get('containerStatuses',[]) if x['name']=='workload'),{})
   evidence=json.loads(r.evidence_json)
   if cs.get('state',{}).get('terminated',{}).get('exitCode')==0 and evidence.get('kernels_delta',0)>0 and evidence.get('charged_delta_us',0)>0 and evidence.get('pod_uid')==pod['metadata']['uid'] and evidence.get('container_id')==cs.get('containerID'):self.store.transition(r.workload_id,'COMPLETED','CUDA execution and normal completion verified')
   else:self.store.transition(r.workload_id,'FAILED','completion lacks verified execution/identity/exit0')
   return
  admitted=condition(w,'Admitted').get('status')=='True'
  if not admitted:
   nodes=self.kube.list_nodes();flavor=self.kube.get_flavor(FLAVOR);target=self.exports.observe_target(r.plan.target,self.clock)
   v=evaluate_admission(r,job,w,nodes,flavor,target,self.clock(),expected_image=self.settings.image)
   own=next((x for x in w.get('status',{}).get('admissionChecks',[]) if x['name']==CHECK),{})
   patch=own_check_patch(w,v)
   entry=patch['status']['admissionChecks'][0]
   if v.state=='Ready':self.store.record_admission(r.workload_id,v.epoch,v.keti_decision_id)
   if own.get('state')!=entry['state'] or own.get('message')!=entry['message']:
    # A 409/ownership conflict is retried only by the next full fresh reconciliation.
    self.kube.patch_check(w,patch)
   if v.state=='Rejected':raise ContractError('FAILED_PRECONDITION',v.reason)
   if v.state!='Ready':self.store.transition(r.workload_id,'KETI_WAIT' if condition(w,'QuotaReserved').get('status')=='True' else 'QUOTA_WAIT',v.reason,epoch='',keti_decision_id='',guard_decision_id='',evidence_json='{}',sample_json='{}')
   return
  own=next((x for x in w.get('status',{}).get('admissionChecks',[]) if x['name']==CHECK),{})
  try:verify_admitted_approval(r,w)
  except (ContractError,ValueError,KeyError,TypeError) as exc:
   verdict=AdmissionVerdict('Rejected','REPLAN_REQUIRED: '+str(exc))
   self.kube.patch_check(w,own_check_patch(w,verdict))
   raise ContractError('FAILED_PRECONDITION',verdict.reason)
  if len(pods)!=1:return
  pod=pods[0]
  try:domain=self.exports.read_domain(r.plan.target,pods,self.clock)
  except (OSError,ValueError) as exc:
   log.warning(canonical(dict(event='runtime_observation_unavailable',workload_id=r.workload_id,error=type(exc).__name__)))
   return
  a=assess_apply(r,job,pod,domain,r.sample_json,self.clock())
  if json.loads(a.evidence_json):
   ev=json.loads(a.evidence_json);ev.update(workload_uid=w['metadata']['uid'],trace_id=r.plan.trace_id,profile_id=r.profile.profile_id,profile_version=r.profile.version,plan_id=r.plan.plan_id,plan_revision=r.plan.revision,keti_decision_id=r.keti_decision_id,guard_decision_id=r.guard_decision_id,admission_epoch=r.epoch)
   ev.pop('evidence_digest',None);ev['evidence_digest']=sha(canonical(ev));a=replace(a,evidence_json=canonical(ev))
  # Preserve an already proven execution interval if a later observation is stationary.
  if json.loads(r.evidence_json).get('kernels_delta',0)>0 and a.state!='EXECUTION_OBSERVED':a=replace(a,state='EXECUTION_OBSERVED',evidence_json=r.evidence_json)
  self.store.record_assessment(r.workload_id,a)
