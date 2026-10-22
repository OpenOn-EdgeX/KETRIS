"""Independent artifact verifier: receipts and placement alone can never pass."""
from dataclasses import dataclass,asdict
from datetime import datetime
from pathlib import Path
import argparse,base64,hashlib,json
@dataclass(frozen=True)
class VerificationResult:
 passed:bool;checks:dict;limits:list;artifacts:list;measurements:list;errors:dict

def need(ok,reason):
 if not ok:raise ValueError(reason)
def stamp(s):return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def link_matches(state,evidence):return all(bool(state.get(a)) and state[a]==evidence.get(b) for a,b in [('job_uid','job_uid'),('config_digest','approved_config_digest'),('admission_epoch','admission_epoch')])
def measure_window(start,end,pod_uids):
 need(bool(start.get('mig_uuid')) and start.get('mig_uuid')==end.get('mig_uuid') and start.get('generation')==end.get('generation'),'partition identity/generation mismatch')
 elapsed=stamp(end['observed_at'])-stamp(start['observed_at']);need(elapsed>=20,'native measurement window shorter than20s')
 before={x['pod_uid']:x for x in start['tenants'] if x.get('phase')=='armed'};after={x['pod_uid']:x for x in end['tenants'] if x.get('phase')=='armed'}
 need(set(before)==set(after)==set(pod_uids),'active cohort mismatch');workers=[]
 for uid,weight,budget in zip(pod_uids,[3,1],[7500,2500]):
  a,b=before[uid],after[uid]
  need(all(a.get(k)==b.get(k) and a.get(k) for k in ('runtime_generation','allocation_id','pod_uid','container_id')),'tenant runtime identity changed')
  need(a['space_percent']==b['space_percent']==50 and a['time_weight']==b['time_weight']==weight and a['budget_us']==b['budget_us']==budget,'applied policy mismatch')
  charged=b['charged_total_us']-a['charged_total_us'];kernels=b['kernels']-a['kernels'];need(charged>0 and kernels>0,'no execution counter progress')
  workers.append(dict(pod_uid=uid,container_id=a['container_id'],runtime_generation=a['runtime_generation'],allocation_id=a['allocation_id'],charged_us=charged,kernels=kernels,iterations_per_second=kernels/elapsed,reported_sm_count=b['reported_sm_count']))
 share=workers[0]['charged_us']/sum(x['charged_us'] for x in workers)*100
 return dict(elapsed_s=elapsed,a_share_percent=share,error_pp=abs(share-75),workers=workers)
def read_rows(path):return [json.loads(x) for x in path.read_text().splitlines() if x.startswith('{')]
def true_condition(obj,name):return any(x.get('type')==name and x.get('status')=='True' for x in obj.get('status',{}).get('conditions',[]))
def verify_result(root:Path)->VerificationResult:
 checks={};errors={};measurements=[]
 def load(n):return json.loads((root/n).read_text())
 def check(name,fn):
  try:fn();checks[name]=True
  except (OSError,ValueError,KeyError,TypeError,StopIteration,IndexError) as e:checks[name]=False;errors[name]=str(e)
 def negative():
  info=load('run-info.json');rows=read_rows(root/('client-negative-'+info['run_id']+'.log'));cases={x['case']:x for x in rows if x.get('event')=='negative_rpc'}
  for name in ('missing_auth','unknown_units','expired','cap','weight','uuid_key_mismatch','payload_conflict','profile_wait','contract_only_state','missing_version_state'):
   need(cases[name]['actual']==cases[name]['expected'],'negative failed: '+name)
  need(cases['profile_wait']['response']['state']=='PROFILE_WAIT','wrong reverse-order wait')
  need(cases['contract_only_state']['response']['state']=='PLAN_VALIDATED' and not cases['contract_only_state']['response'].get('job_uid'),'contract-only launched')
  need(cases['missing_version_state']['response']['state']=='PROFILE_WAIT','missing profile version not waiting')
  need(cases['late_profile']['response']['receipt_id']==cases['duplicate_profile']['response']['receipt_id'],'duplicate receipt changed')
  before=load('negative-jobs-before.json')['items'] if (root/'negative-jobs-before.json').exists() else []
  need({j['metadata']['uid'] for j in before}=={j['metadata']['uid'] for j in load('negative-jobs.json')['items']},'negative inputs created an executable Job')
 def guard():
  d=load('guard-rejected.json');w=d['workload'];ac={x['name']:x for x in w['status']['admissionChecks']}
  need(ac['keti-scheduler']['state']=='Ready' and ac['ketris-plan-validation']['state']=='Rejected','dual guard negative not observed');need(not d['pods'] and d['job']['spec']['suspend'] and not true_condition(w,'Admitted'),'negative guard launched a Pod')
 def held():
  d=load('queue-held.json');run=load('run-info.json')['run_id'];jobs=d['jobs']['items']
  for letter in ('a','b'):
   j=next(j for j in jobs if j['metadata']['annotations']['ketris.integration/workload-id']==run+'-'+letter);uid=j['metadata']['uid'];need(j['spec']['suspend'],'quota0 job unsuspended');need(not any(any(o.get('uid')==uid for o in p['metadata'].get('ownerReferences',[])) for p in d['pods']['items']),'quota0 Pod exists')
 def rpc():
  from google.protobuf.json_format import MessageToDict
  from ketris_integration.rpc import institution_bridge_pb2 as pb
  run=load('run-info.json')['run_id'];peers=set()
  for letter in ('a','b'):
   for role,cls,provider in [('yonsei',pb.PushProfileRequest,'yonsei-mock'),('skku',pb.SubmitPartitionPlanRequest,'skku-mock')]:
    name=role+'-'+run+'-'+letter;rows=read_rows(root/('client-'+name+'.log'));req=next(x for x in rows if x.get('event')=='rpc_request');response=next(x for x in rows if x.get('event')=='rpc_response');message=cls.FromString(base64.b64decode(req['protobuf_b64']));d=MessageToDict(message,preserving_proto_field_name=True)
    digest=hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest();need(d==req['request'] and digest==req['payload_sha256']==response['payload_sha256']==response['response']['payload_sha256'],'RPC binary/hash linkage mismatch')
    need(d['meta']['origin']=='SYNTHETIC' and d['meta']['execution_mode']=='LIVE_EXECUTION' and d['meta']['provider']==provider,'institution provenance mismatch')
    pods=load('client-'+name+'-pods.json')['items'];need(len(pods)==1 and req['client_pod_uid']==pods[0]['metadata']['uid'],'client Pod identity mismatch');peers.add(req['client_pod_uid'])
  need(len(peers)==4,'institutions did not run in distinct client Pods')
 def approvals():
  wls=load('workloads-admitted.json')['items']
  for letter in ('a','b'):
   st=load('state-'+letter+'-final.json');w=next(w for w in wls if any(o.get('uid')==st['job_uid'] for o in w['metadata'].get('ownerReferences',[])));ac={x['name']:x for x in w['status']['admissionChecks']};need(ac['keti-scheduler']['state']==ac['ketris-plan-validation']['state']=='Ready' and true_condition(w,'Admitted'),'actual dual admission missing')
   evidence=json.loads(st['applied']['evidence_json']);need(link_matches(st,evidence),'approved digest/UID/epoch mismatch');need(evidence['workload_uid']==w['metadata']['uid'],'Workload UID mismatch')
 def progress():
  states=[load('state-'+x+'-final.json') for x in ('a','b')];uids=[s['applied']['pod_uid'] for s in states];windows=load('windows.json');need(len(windows)==3,'three windows required')
  for window in windows:
   row=measure_window(window['start'],window['end'],uids);need(row['error_pp']<=1,'time share exceeds1pp');measurements.append(row)
   for worker,state in zip(row['workers'],states):
    ev=json.loads(state['applied']['evidence_json']);need(link_matches(state,ev) and ev['kernels_delta']>0 and ev['charged_delta_us']>0,'missing bridge progress proof');need(worker['runtime_generation']==ev['runtime_generation'] and worker['container_id']==ev['container_id'],'measurement/bridge runtime mismatch')
 def completion():
  jobs=load('jobs-final.json')['items'];pods=load('pods-final.json')['items'];wls=load('workloads-final.json')['items'];target=load('target.json')
  for letter in ('a','b'):
   state=load('state-'+letter+'-final.json');need(state['state']=='COMPLETED','bridge not completed');j=next(j for j in jobs if j['metadata']['uid']==state['job_uid']);need(true_condition(j,'Complete'),'Job incomplete');pod=next(p for p in pods if p['metadata']['uid']==state['applied']['pod_uid']);cs=next(c for c in pod['status']['containerStatuses'] if c['name']=='workload');need(cs['state']['terminated']['exitCode']==0 and pod['spec']['schedulerName']=='default-scheduler','Pod not normal/default scheduled');need(any(true_condition(w,'Finished') for w in wls if any(o.get('uid')==j['metadata']['uid'] for o in w['metadata']['ownerReferences'])),'Kueue not Finished')
   rows=read_rows(root/('worker-'+letter+'.log'));identity=next(x for x in rows if x.get('event')=='identity');done=next(x for x in rows if x.get('event')=='complete');need(identity['actual_cuda_uuid']==target['mig_uuid'] and identity['device_count']==1 and identity['pod_uid']==pod['metadata']['uid'] and done['iterations']>0 and done['elapsed_s']>=120,'actual worker CUDA proof missing')
  need(not any(t.get('phase')=='armed' and t.get('pod_uid') in {s['applied']['pod_uid'] for s in [load('state-a-final.json'),load('state-b-final.json')]} for t in load('partition-after.json')['tenants']),'execution tenants not released');need('Enabled' in (root/'mig-mode.txt').read_text(),'MIG no longer Enabled')
 def live_dashboard():
  snapshot=load('dashboard-active.json');states=load('states-executing.json');uids={x['applied']['pod_uid'] for x in states};parts=snapshot['partitions'];target=load('target.json')['mig_uuid']
  need(bool(snapshot.get('captured_at')) and len(uids)==2 and uids<={w.get('pod_uid') for w in snapshot['workloads']},'live dashboard missing actual executing Pods')
  migs={p['metrics']['mig_uuid']['value'] for p in parts if p['kind']=='mig'};need(target in migs and len(migs)>=2,'live dashboard missing selected/other MIG inventory')
  lo=stamp(load('windows.json')[0]['start']['observed_at'])-15;hi=stamp(load('windows.json')[-1]['end']['observed_at'])+15;need(lo<=stamp(snapshot['captured_at'])<=hi,'live dashboard timestamp outside measured run')
 def preserved():need(load('preflight-before-hardware.json')['shared_fingerprints']==load('postflight.json')['shared_fingerprints'],'shared config changed')
 def durable():
  old,new=load('state-before-restart.json'),load('state-after-restart.json');need([(x['job_uid'],x['config_digest'],x['state']) for x in old]==[(x['job_uid'],x['config_digest'],x['state']) for x in new],'restart changed execution identity')
  run=load('run-info.json')['run_id']
  for letter in ('a','b'):
   a=next(x['response'] for x in read_rows(root/('client-yonsei-'+run+'-'+letter+'.log')) if x.get('event')=='rpc_response');b=next(x['response'] for x in read_rows(root/('client-duplicate-'+run+'-'+letter+'.log')) if x.get('event')=='rpc_response');need(a==b,'restarted server changed receipt')
 for name,fn in [('negative_rpc',negative),('guard_rejection',guard),('quota_zero',held),('actual_rpc',rpc),('dual_admission',approvals),('execution_progress',progress),('normal_completion',completion),('live_dashboard',live_dashboard),('shared_preserved',preserved),('durable_replay',durable)]:check(name,fn)
 artifacts=[dict(path=str(p.relative_to(root)),sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size) for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ('verification.json','verification-report.md','evidence-manifest.json')]
 return VerificationResult(all(checks.values()),checks,['Institution inputs are synthetic; this is not an official institution protocol or optimization algorithm.','Direct GEMM; no new LLM/NPU execution.','Explicit exact-MIG queue; existing Router automatic MIG routing and RPM billing are not validated.','Charged kernel wall time, not exclusive SM ownership or physical GPU utilization.','Cross-node decision mismatch is unit-tested; the actual negative guard probe is operator annotation drift.'],artifacts,measurements,errors)
def main():
 p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();r=verify_result(a.root);(a.root/'verification.json').write_text(json.dumps(asdict(r),indent=2));(a.root/'evidence-manifest.json').write_text(json.dumps(r.artifacts,indent=2));print(json.dumps(asdict(r),indent=2));raise SystemExit(0 if r.passed else 1)
if __name__=='__main__':main()
