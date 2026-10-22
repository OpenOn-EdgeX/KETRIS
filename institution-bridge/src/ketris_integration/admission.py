"""Exact-plan check coexists with, and never overwrites, KETI's decision."""
from datetime import datetime,timezone
from .contracts import *
from .jobs import build_job,project_execution_contract,NAMESPACE
CHECK='ketris-plan-validation';FLAVOR='ketris-integration-devmaster'
def condition(w,t):return next((c for c in w.get('status',{}).get('conditions',[]) if c['type']==t),{})
def admission_epoch(w,keti_decision_id,config_digest):
 return sha(canonical([w['metadata']['uid'],condition(w,'QuotaReserved').get('lastTransitionTime'),w.get('status',{}).get('admission'),keti_decision_id,config_digest]))
def evaluate_admission(record,job,workload,nodes,flavor,target,now,expected_image='ketris-institution-gemm:20261015-v1')->AdmissionVerdict:
 pending=lambda why:AdmissionVerdict('Pending',why)
 rejected=lambda why:AdmissionVerdict('Rejected','REPLAN_REQUIRED: '+why)
 if condition(workload,'QuotaReserved').get('status')!='True' or condition(workload,'Evicted').get('status')=='True':return pending('quota not reserved or evicted')
 existing=next((x for x in workload.get('status',{}).get('admissionChecks',[]) if x['name']=='keti-scheduler'),{})
 if existing.get('state')!='Ready':return pending('waiting for existing KETI Ready')
 p,q=record.profile,record.plan
 try:
  require(p and q and p.executable and q.executable,'executable inputs missing')
  require(min(int(p.expires_at_unix_s),int(q.expires_at_unix_s))>now.timestamp(),'inputs expired before Ready')
  require(p.profile_id==q.profile_id and p.version==q.profile_version and p.workload_id==q.workload_id and p.trace_id==q.trace_id,'profile reference mismatch')
  require(record.digest in ('',config_digest(p,q)),'config digest mismatch')
  require(job['metadata']['uid']==record.job_uid,'Job UID mismatch')
  wm=workload['metadata'];require(wm['namespace']==NAMESPACE and any(x.get('uid')==record.job_uid and x.get('kind')=='Job' for x in wm.get('ownerReferences',[])),'Workload ownership mismatch')
  desired=project_execution_contract(build_job(record,expected_image))
  require(project_execution_contract(job)==desired,'Job controlled contract drift')
  sets=workload['spec']['podSets'];require(len(sets)==1 and sets[0].get('count')==1,'unexpected podSets')
  require(project_execution_contract(sets[0]['template'])==desired,'Workload controlled contract drift')
  assignments=workload['status']['admission']['podSetAssignments'];require(len(assignments)==1,'unexpected assignments')
  a=assignments[0];require(a['name']==sets[0]['name'] and a.get('count',1)==1,'assignment mismatch')
  require(set(a['flavors'])=={'cpu','memory',q.target.resource_name} and set(a['flavors'].values())=={FLAVOR},'exact resource/flavor mismatch')
  require(int(a['resourceUsage'][q.target.resource_name])==q.space_percent,'MIG quota mismatch')
  require(flavor['spec'].get('nodeLabels')=={'kubernetes.io/hostname':q.target.node},'flavor no longer target-only')
  advertised=[n['metadata']['name'] for n in nodes if q.target.resource_name in n.get('status',{}).get('allocatable',{})]
  require(advertised==[q.target.node],'target resource not unique on chosen node')
  if not target.available:return pending('target export unavailable: '+target.reason)
  require(target.generation==q.target.inventory_generation,'inventory generation mismatch')
  inventory=json.loads(target.inventory_json)
  require(inventory.get('gpu_uuid')==q.target.gpu_uuid,'GPU identity mismatch')
  require(p.gpu_memory_bytes<=int(inventory.get('memory_mib',0))*1048576,'profile exceeds MIG memory capacity')
  if target.manager_state!='running':return pending('target manager not running')
  updates=existing.get('podSetUpdates',[]);require(len(updates)==1 and updates[0]['name']==sets[0]['name'],'KETI podSet mismatch')
  u=updates[0];require(u.get('nodeSelector',{}).get('kubernetes.io/hostname')==q.target.node,'KETI selected a different node')
  decision=u.get('labels',{}).get('keti.re.kr/decision-id');require(bool(decision),'KETI decision missing')
  epoch=admission_epoch(workload,decision,config_digest(p,q))
  return AdmissionVerdict('Ready','exact plan and existing KETI decision match',epoch,decision,'guard-'+sha(epoch)[:20])
 except (ContractError,KeyError,ValueError,TypeError) as exc:return rejected(str(exc))
def own_check_patch(workload,verdict)->dict:
 return dict(apiVersion='kueue.x-k8s.io/v1beta2',kind='Workload',metadata={'name':workload['metadata']['name'],'namespace':workload['metadata']['namespace'],'uid':workload['metadata']['uid'],'resourceVersion':workload['metadata']['resourceVersion']},status={'admissionChecks':[dict(name=CHECK,state=verdict.state,message=canonical(dict(reason=verdict.reason,epoch=verdict.epoch,decision=verdict.guard_decision_id)),lastTransitionTime=datetime.now(timezone.utc).isoformat())]})

def verify_admitted_approval(record,workload):
 """A stored Ready is valid only for this current reservation and KETI decision."""
 require(condition(workload,'QuotaReserved').get('status')=='True' and condition(workload,'Evicted').get('status')!='True','admitted reservation changed or evicted')
 checks={x['name']:x for x in workload.get('status',{}).get('admissionChecks',[])};keti=checks.get('keti-scheduler',{});own=checks.get(CHECK,{})
 require(keti.get('state')=='Ready' and own.get('state')=='Ready','admitted checks are not both Ready')
 updates=keti.get('podSetUpdates',[]);require(len(updates)==1,'admitted KETI updates changed')
 update=updates[0];sets=workload['spec']['podSets'];require(len(sets)==1 and update.get('name')==sets[0]['name'],'admitted podSet changed')
 require(update.get('nodeSelector',{}).get('kubernetes.io/hostname')==record.plan.target.node,'admitted KETI selected a different node')
 decision=update.get('labels',{}).get('keti.re.kr/decision-id');epoch=admission_epoch(workload,decision,config_digest(record.profile,record.plan))
 require(bool(decision) and decision==record.keti_decision_id and epoch==record.epoch,'admitted decision or epoch changed')
 message=json.loads(own.get('message','{}'));require(message.get('epoch')==epoch and message.get('decision')==record.guard_decision_id=='guard-'+sha(epoch)[:20],'admitted own-check message does not match persisted epoch')
