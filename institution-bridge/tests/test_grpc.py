import unittest,tempfile,grpc,json,time
from pathlib import Path
from fixtures import profile_dict,plan_dict
from ketris_integration.rpc import institution_bridge_pb2 as pb,institution_bridge_pb2_grpc as stubs
from ketris_integration.store import BridgeStore
class RpcTests(unittest.TestCase):
 def setUp(self):
  from ketris_integration.grpc_service import make_grpc_server
  self.make=make_grpc_server;self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'db');self.store=BridgeStore(self.path);self.server=make_grpc_server(self.store,'test-token');self.port=self.server.add_insecure_port('127.0.0.1:0');self.server.start();self.channel=grpc.insecure_channel(f'127.0.0.1:{self.port}');self.ps=stubs.ProfileIngestServiceStub(self.channel);self.qs=stubs.PlanAdmissionServiceStub(self.channel);self.ss=stubs.IntegrationStatusServiceStub(self.channel);self.auth=(('authorization','Bearer test-token'),)
  self.p=profile_dict();self.q=plan_dict()
  for d in (self.p,self.q):d['meta'].update(created_at_unix_s=int(time.time())-1,expires_at_unix_s=int(time.time())+3600)
 def tearDown(self):self.channel.close();self.server.stop(0).wait();self.store.close();self.tmp.cleanup()
 def test_real_rpc_profile_get_plan_state_and_events(self):
  r=self.ps.PushProfile(pb.PushProfileRequest(**self.p),metadata=self.auth,timeout=3);self.assertEqual(r.state,pb.RECEIVED)
  fetched=self.ps.GetProfile(pb.GetProfileRequest(profile_id='profile-a',version=1),metadata=self.auth,timeout=3);self.assertEqual(fetched.profile.model,'cuda-gemm-2048-fp32-v1')
  self.qs.SubmitPartitionPlan(pb.SubmitPartitionPlanRequest(**self.q),metadata=self.auth,timeout=3)
  state=self.ss.GetWorkloadState(pb.GetWorkloadStateRequest(workload_id='work-a'),metadata=self.auth,timeout=3);self.assertEqual(state.state,'PLAN_VALIDATED');self.assertFalse(state.job_uid)
  events=self.ss.ListEvents(pb.ListEventsRequest(trace_id='trace-1'),metadata=self.auth,timeout=3);self.assertEqual(len(events.events),2)
 def test_auth_error_and_contract_error_codes(self):
  with self.assertRaises(grpc.RpcError) as x:self.ps.PushProfile(pb.PushProfileRequest(**self.p),timeout=3)
  self.assertEqual(x.exception.code(),grpc.StatusCode.UNAUTHENTICATED);self.assertEqual(self.store.list_executions(),[])
  self.p['gpu_memory_unit']='undefined'
  with self.assertRaises(grpc.RpcError) as x:self.ps.PushProfile(pb.PushProfileRequest(**self.p),metadata=self.auth,timeout=3)
  self.assertEqual(x.exception.code(),grpc.StatusCode.INVALID_ARGUMENT)
 def test_replay_and_server_restart_preserve_job_identity(self):
  request=pb.PushProfileRequest(**self.p);first=self.ps.PushProfile(request,metadata=self.auth,timeout=3);self.assertEqual(first,self.ps.PushProfile(request,metadata=self.auth,timeout=3))
  self.server.stop(0).wait();self.store.close();self.store=BridgeStore(self.path);self.server=self.make(self.store,'test-token');port=self.server.add_insecure_port('127.0.0.1:0');self.server.start()
  with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:self.assertEqual(first,stubs.ProfileIngestServiceStub(channel).PushProfile(request,metadata=self.auth,timeout=3))
 def test_identical_profile_and_plan_replay_after_expiry_and_reopen(self):
  from datetime import datetime,timezone,timedelta
  from unittest.mock import patch
  profile=pb.PushProfileRequest(**self.p);plan=pb.SubmitPartitionPlanRequest(**self.q)
  rp=self.ps.PushProfile(profile,metadata=self.auth,timeout=3);rq=self.qs.SubmitPartitionPlan(plan,metadata=self.auth,timeout=3)
  self.server.stop(0).wait();self.store.close();self.store=BridgeStore(self.path);self.server=self.make(self.store,'test-token');port=self.server.add_insecure_port('127.0.0.1:0');self.server.start()
  with grpc.insecure_channel(f'127.0.0.1:{port}') as channel,patch('ketris_integration.grpc_service.datetime') as dt:
   dt.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2);ps=stubs.ProfileIngestServiceStub(channel);qs=stubs.PlanAdmissionServiceStub(channel)
   self.assertEqual(ps.PushProfile(profile,metadata=self.auth,timeout=3),rp)
   self.assertEqual(qs.SubmitPartitionPlan(plan,metadata=self.auth,timeout=3),rq)
   for original,method,field in [(profile,ps.PushProfile,'gpu_memory_value'),(plan,qs.SubmitPartitionPlan,'time_weight')]:
    changed=type(original)();changed.CopyFrom(original);setattr(changed,field,2)
    with self.assertRaises(grpc.RpcError) as error:method(changed,metadata=self.auth,timeout=3)
    self.assertEqual(error.exception.code(),grpc.StatusCode.ALREADY_EXISTS)
    changed.meta.request_id+='-new'
    with self.assertRaises(grpc.RpcError) as error:method(changed,metadata=self.auth,timeout=3)
    self.assertEqual(error.exception.code(),grpc.StatusCode.INVALID_ARGUMENT)
