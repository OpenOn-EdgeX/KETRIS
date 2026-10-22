"""Actual RPC fault cases, executed in an isolated CPU client Job."""
import base64,copy,json,os,time,grpc
from pathlib import Path
from ketris_integration.rpc import institution_bridge_pb2 as p,institution_bridge_pb2_grpc as s
from ketris_integration.contracts import canonical,wire_dict,payload_digest
now=int(time.time());run=os.environ['RUN_ID'];wid=run+'-negative';trace='trace-'+run;target=json.loads(Path('/inputs/target.json').read_text());auth=(('authorization','Bearer '+Path('/auth/token').read_text().strip()),)
channel=grpc.insecure_channel('ketris-institution-bridge.ketris-integration.svc:50051');ps=s.ProfileIngestServiceStub(channel);qs=s.PlanAdmissionServiceStub(channel);ss=s.IntegrationStatusServiceStub(channel)
meta=dict(schema_version='v1',request_id='negative-profile:'+run,trace_id=trace,workload_id=wid,provider='yonsei-mock',origin='SYNTHETIC',execution_mode='CONTRACT_ONLY',created_at_unix_s=now,expires_at_unix_s=now+3600)
profile=dict(meta=meta,profile_id='profile-'+wid,version=1,model='cuda-gemm-2048-fp32-v1',workload_type='benchmark',input_shape=[2048,2048],compute_dtype='fp32',input_dtype='fp32',batch=1,gpu_memory_value=1,gpu_memory_unit='GiB',gpu_memory_source='SYNTHETIC')
plan=dict(meta=meta|dict(request_id='negative-plan:'+run,provider='skku-mock'),plan_id='plan-'+wid,revision=1,profile_id='profile-'+wid,profile_version=1,assignment_id='a',target=target,space_percent=50,time_weight=3,policy_source='SYNTHETIC_TEST_POLICY',snapshot_ref='operator-live-inventory',execution_template='cuda-gemm-v1')
def call(case,fn,request,expected='OK',metadata=auth):
 row=dict(event='negative_rpc',case=case,expected=expected,request=wire_dict(request),protobuf_b64=base64.b64encode(request.SerializeToString(deterministic=True)).decode(),payload_sha256=payload_digest(request))
 try:r=fn(request,metadata=metadata,timeout=10);row.update(actual='OK',response=wire_dict(r))
 except grpc.RpcError as e:row.update(actual=e.code().name,reason=e.details())
 row['passed']=row['actual']==expected;print(canonical(row),flush=True)
 if not row['passed']:raise AssertionError(case)
 return row
call('missing_auth',ps.PushProfile,p.PushProfileRequest(**profile),'UNAUTHENTICATED',())
for case,field,value in [('unknown_units','gpu_memory_unit','unknown'),('nonfinite','predicted_latency_ms',-1)]:
 d=copy.deepcopy(profile);d[field]=value;call(case,ps.PushProfile,p.PushProfileRequest(**d),'INVALID_ARGUMENT')
d=copy.deepcopy(profile);d['meta']['expires_at_unix_s']=now-1;call('expired',ps.PushProfile,p.PushProfileRequest(**d),'INVALID_ARGUMENT')
for case,field,value in [('cap','space_percent',101),('weight','time_weight',0)]:
 d=copy.deepcopy(plan);d[field]=value;call(case,qs.SubmitPartitionPlan,p.SubmitPartitionPlanRequest(**d),'INVALID_ARGUMENT')
d=copy.deepcopy(plan);d['target']['resource_name']='keti.re.kr/mig-invalid-share';call('uuid_key_mismatch',qs.SubmitPartitionPlan,p.SubmitPartitionPlanRequest(**d),'INVALID_ARGUMENT')
call('plan_before_profile',qs.SubmitPartitionPlan,p.SubmitPartitionPlanRequest(**plan))
call('profile_wait',ss.GetWorkloadState,p.GetWorkloadStateRequest(workload_id=wid))
call('late_profile',ps.PushProfile,p.PushProfileRequest(**profile));first=call('duplicate_profile',ps.PushProfile,p.PushProfileRequest(**profile))
d=copy.deepcopy(profile);d['gpu_memory_value']=2;call('payload_conflict',ps.PushProfile,p.PushProfileRequest(**d),'ALREADY_EXISTS')
d=copy.deepcopy(plan);d['meta'].update(request_id='version-missing:'+run,workload_id=wid+'-version');d['plan_id']+='-version';d['profile_version']=99;call('missing_profile_version',qs.SubmitPartitionPlan,p.SubmitPartitionPlanRequest(**d))
call('contract_only_state',ss.GetWorkloadState,p.GetWorkloadStateRequest(workload_id=wid));call('missing_version_state',ss.GetWorkloadState,p.GetWorkloadStateRequest(workload_id=wid+'-version'))
print(canonical(dict(event='negative_complete',client_pod_uid=os.environ['POD_UID'],workload_id=wid)),flush=True)
