"""Mock institutional clients and read-only status CLI. Transport is real gRPC."""
from pathlib import Path
import argparse,base64,json,os,time
import grpc
from google.protobuf import text_format
from google.protobuf.json_format import ParseDict
from .contracts import canonical,wire_dict,payload_digest
from .rpc import institution_bridge_pb2 as pb,institution_bridge_pb2_grpc as stub

def main():
 p=argparse.ArgumentParser();p.add_argument('--address',default='ketris-institution-bridge.ketris-integration.svc:50051');p.add_argument('--token-file',default='/auth/token');p.add_argument('--file');p.add_argument('--workload');p.add_argument('--trace');p.add_argument('--target-file');p.add_argument('--weight',type=int,default=3);p.add_argument('--cap',type=int,default=50);p.add_argument('--profile-version',type=int,default=1);p.add_argument('--out-dir',default='/tmp/requests');p.add_argument('command',choices=['push-profile','get-profile','submit-plan','state','events','yonsei-demo','skku-demo']);a=p.parse_args()
 token=Path(a.token_file).read_text().strip();auth=(('authorization','Bearer '+token),)
 now=int(time.time());wid=a.workload or '';trace=a.trace or 'trace-'+wid
 meta=dict(schema_version='v1',request_id='profile:'+wid,trace_id=trace,workload_id=wid,provider='yonsei-mock',origin='SYNTHETIC',execution_mode='LIVE_EXECUTION',created_at_unix_s=now,expires_at_unix_s=now+3600)
 with grpc.insecure_channel(a.address) as channel:
  ps=stub.ProfileIngestServiceStub(channel);qs=stub.PlanAdmissionServiceStub(channel);ss=stub.IntegrationStatusServiceStub(channel)
  if a.command in ('yonsei-demo','push-profile'):
   cls=pb.PushProfileRequest;fn=ps.PushProfile;request=cls(meta=pb.MessageMeta(**meta),profile_id='profile-'+wid,version=a.profile_version,model='cuda-gemm-2048-fp32-v1',workload_type='benchmark',input_shape=[2048,2048],compute_dtype='fp32',input_dtype='fp32',batch=1,gpu_memory_value=1,gpu_memory_unit='GiB',gpu_memory_source='SYNTHETIC')
  elif a.command in ('skku-demo','submit-plan'):
   cls=pb.SubmitPartitionPlanRequest;fn=qs.SubmitPartitionPlan
   if a.command=='skku-demo':
    profile=ps.GetProfile(pb.GetProfileRequest(profile_id='profile-'+wid,version=a.profile_version),metadata=auth,timeout=10);print(canonical(dict(event='profile_read',profile=wire_dict(profile))),flush=True)
   target=json.loads(Path(a.target_file).read_text()) if a.target_file else {}
   request=cls(meta=pb.MessageMeta(**(meta|dict(request_id='plan:'+wid,provider='skku-mock'))),plan_id='plan-'+wid,revision=1,profile_id='profile-'+wid,profile_version=a.profile_version,assignment_id='assignment-'+wid,target=pb.LiveMigTarget(**target),space_percent=a.cap,time_weight=a.weight,policy_source='SYNTHETIC_TEST_POLICY',snapshot_ref='operator-live-inventory',execution_template='cuda-gemm-v1')
  elif a.command=='get-profile':cls=pb.GetProfileRequest;request=cls(profile_id='profile-'+wid,version=a.profile_version);fn=ps.GetProfile
  elif a.command=='state':cls=pb.GetWorkloadStateRequest;request=cls(workload_id=wid);fn=ss.GetWorkloadState
  else:cls=pb.ListEventsRequest;request=cls(trace_id=trace,workload_id=wid,limit=500);fn=ss.ListEvents
  if a.file:
   raw=Path(a.file).read_text();request=cls()
   ParseDict(json.loads(raw),request) if a.file.endswith('.json') else text_format.Parse(raw,request)
  req=wire_dict(request);digest=payload_digest(request)
  print(canonical(dict(event='rpc_request',command=a.command,client_pod=os.environ.get('POD_NAME'),client_pod_uid=os.environ.get('POD_UID'),request=req,payload_sha256=digest,protobuf_b64=base64.b64encode(request.SerializeToString(deterministic=True)).decode())),flush=True)
  response=fn(request,metadata=auth,timeout=10)
  print(canonical(dict(event='rpc_response',command=a.command,payload_sha256=digest,response=wire_dict(response),protobuf_b64=base64.b64encode(response.SerializeToString(deterministic=True)).decode())),flush=True)
  out=Path(a.out_dir);out.mkdir(parents=True,exist_ok=True);(out/(a.command+'-request.json')).write_text(canonical(req));(out/(a.command+'-response.json')).write_text(canonical(wire_dict(response)))
if __name__=='__main__':main()
