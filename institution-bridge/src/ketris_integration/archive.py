"""Offline adapter: only independently verified windows enter dashboard replay."""
from pathlib import Path
import hashlib,json,statistics
from .result_validation import verify_result

def profile_origin(profile):
 if profile.get('meta',{}).get('origin')!='SYNTHETIC' or profile.get('gpu_memory_source')!='SYNTHETIC':raise ValueError('this adapter requires explicit synthetic provenance')
 return 'SYNTHETIC 기관 입력 · 실제 RPC/GPU 실행'
def make_replay_bundle(root:Path):
 result=verify_result(root)
 if not result.passed:raise ValueError('unverified institution run: '+str(result.errors))
 from ketris_dashboard.archive import ExperimentBundle
 from ketris_dashboard.models import Device,Partition,Workload,ViewSnapshotV1,ReplayFrame,SourceStatus,observed
 load=lambda n:json.loads((root/n).read_text());info=load('run-info.json');target=load('target.json');windows=load('windows.json');states=[load('state-'+x+'-final.json') for x in ('a','b')];all_parts=load('all-partitions-active.json');gpu='archive:'+target['gpu_uuid'];frames=[];evidence={};manifest=[]
 def source(name,projection):
  raw=(root/name).read_bytes();digest=hashlib.sha256(raw).hexdigest();manifest.append(dict(path=name,sha256=digest,bytes=len(raw)));eid='e-'+digest[:24];evidence[eid]=dict(id=eid,source_path=name,sha256=digest,projection=projection);return eid
 windows_eid=source('windows.json',result.measurements);ids=[windows_eid,source('states-executing.json',load('states-executing.json')),source('guard-rejected.json',load('guard-rejected.json')),source('all-partitions-active.json',all_parts)]
 def metric(v,u=None,q='observed',why=None):return observed(v,u,'archive',quality=q,reason=why)
 parts=[Partition(id='archive:'+p['mig_uuid'],device_id=gpu,kind='mig',name=p['inventory']['profile'],generation=p['generation'],historical=True,metrics={'mig_uuid':metric(p['mig_uuid']),'profile_sm':metric(p['inventory']['profile_sm'],'SM'),'memory_mib':metric(p['inventory']['memory_mib'],'MiB')}) for p in all_parts]
 for i,row in enumerate(result.measurements):
  inventory=windows[i].get('all_start',all_parts)
  parts=[Partition(id='archive:'+p['mig_uuid'],device_id=gpu,kind='mig',name=p['inventory']['profile'],generation=p['generation'],historical=True,metrics={'mig_uuid':metric(p['mig_uuid']),'profile_sm':metric(p['inventory']['profile_sm'],'SM'),'memory_mib':metric(p['inventory']['memory_mib'],'MiB'),'inventory_captured_at':metric(p['observed_at'])}) for p in inventory]
  workers=[]
  for index,(letter,weight,budget) in enumerate([('A',3,7500),('B',1,2500)]):
   state=states[index];ev=json.loads(state['applied']['evidence_json']);detail=json.loads(state['detail_json']);profile=json.loads(detail['profile']['payload_json']);m=row['workers'][index];share=m['charged_us']/sum(w['charged_us'] for w in row['workers'])
   workers.append(Workload(id='archive:'+state['workload_id'],name=letter,partition_id='archive:'+target['mig_uuid'],pod_uid=ev['pod_uid'],container='workload',namespace='ketris-integration-workloads',origin=profile_origin(profile),state='measured',requested={'space_percent':metric(50,'%', 'configured'),'time_weight':metric(weight,'weight','configured')},applied={'mps_percent':metric(50,'%','configured'),'time_share':metric(weight/4,'fraction','configured'),'budget_us':metric(budget,'us/10ms','configured'),'reported_sm_count':metric(ev['reported_sm_count'],'SM'),'actual_cuda_uuid':metric(target['mig_uuid'])},observed={'charged_share':metric(share,'fraction','derived','same-domain charged CUDA kernel wall-time deltas'),'charged_us':metric(m['charged_us'],'us'),'kernels':metric(m['kernels'],'kernels'),'throughput':metric(m['iterations_per_second'],'iter/s')}))
  snapshot=ViewSnapshotV1(node='devmaster · 10.0.1.211 (archive)',mode='replay',captured_at=None,devices=[Device(id=gpu,kind='gpu',name='RTX PRO 6000 Blackwell',node='devmaster')],partitions=parts,workloads=workers,source_status={'archive':SourceStatus(status='ok',reason='Actual institution mock RPC, dual approval, actual MIG GEMM; other MIG inventory captured during run; no invented utilization')})
  frames.append(ReplayFrame(index=i,interval=dict(condition='combined50_50_time3_1',repeat=i+1,start=0,end=row['elapsed_s']),axis_kind='measurement_window',snapshot=snapshot,evidence_ids=ids))
 digest=hashlib.sha256(json.dumps(sorted(manifest,key=lambda x:x['path']),sort_keys=True).encode()).hexdigest()[:16]
 return ExperimentBundle('ketris_institution_grpc_20261015-'+digest,manifest,dict(name='ketris_institution_grpc_20261015',kind='기관 gRPC · MIG',axis_kind='measurement_window',origin='연대·성대 모의 gRPC → KETI → 실제 KETRIS MIG',historical=True,precision_verdict='actual RPC + dual approval +3 measured CUDA windows',limitations=result.limits),{'combined50_50_time3_1':dict(repeats=3,space_pct=[50,50],charged_share_a_mean=statistics.mean(r['a_share_percent']/100 for r in result.measurements),total_ips_mean=statistics.mean(sum(w['iterations_per_second'] for w in r['workers']) for r in result.measurements))},frames,evidence)
