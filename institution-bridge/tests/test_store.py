import unittest,tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from fixtures import *
from ketris_integration.contracts import *
from ketris_integration.rpc import institution_bridge_pb2 as pb
class StoreTests(unittest.TestCase):
 def setUp(self):
  from ketris_integration.store import BridgeStore
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.sqlite');self.cls=BridgeStore;self.s=BridgeStore(self.path)
  self.p=parse_profile(pb.PushProfileRequest(**profile_dict()),NOW);self.q=parse_plan(pb.SubmitPartitionPlanRequest(**plan_dict()),NOW)
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def test_profile_then_plan_and_reverse_order(self):
  self.s.receive_plan(self.q,'q');self.assertEqual(self.s.get_execution('work-a').state,'PROFILE_WAIT');self.assertEqual(self.s.ready_to_submit(NOW),[])
  self.s.receive_profile(self.p,'p');self.assertEqual(len(self.s.ready_to_submit(NOW)),1)
  self.assertEqual(self.s.ready_to_submit(NOW+timedelta(hours=2)),[])
 def test_same_request_returns_same_receipt(self):
  with ThreadPoolExecutor(4) as ex:r=list(ex.map(lambda _:self.s.receive_profile(self.p,'p'),range(8)))
  self.assertEqual(len(set(r)),1);self.assertEqual(self.s.get_execution('work-a').state,'PLAN_WAIT')
 def test_id_or_version_payload_conflict(self):
  self.s.receive_profile(self.p,'p')
  with self.assertRaises(ContractError):self.s.receive_profile(self.p,'different')
  d=profile_dict();d['meta']['request_id']='r-other';d['gpu_memory_value']=2;p=parse_profile(pb.PushProfileRequest(**d),NOW)
  with self.assertRaises(ContractError):self.s.receive_profile(p,'new')
 def test_contract_only_never_enqueues_execution(self):
  d=profile_dict();d['meta']['execution_mode']='CONTRACT_ONLY';p=parse_profile(pb.PushProfileRequest(**d),NOW)
  self.s.receive_profile(p,'p');self.s.receive_plan(self.q,'q');self.assertEqual(self.s.ready_to_submit(NOW),[])
 def test_intent_survives_reopen_before_job_ack(self):
  self.s.receive_profile(self.p,'p');self.s.receive_plan(self.q,'q');self.s.record_job('work-a','uid-1','job-1');self.s.close();self.s=self.cls(self.path)
  self.assertEqual(self.s.get_execution('work-a').job_uid,'uid-1');self.assertEqual(self.s.receive_plan(self.q,'q').object_id,'plan-a')
 def test_event_order_and_terminal_state_not_regressed(self):
  self.s.receive_profile(self.p,'p');self.s.transition('work-a','FAILED','failure');self.s.transition('work-a','PLAN_WAIT','later')
  self.assertEqual(self.s.get_execution('work-a').state,'FAILED')
  events=self.s.events('trace-1',0,100);self.assertEqual([e['seq'] for e in events],sorted({e['seq'] for e in events}))
 def test_profile_revision_cannot_replace_active_execution_input(self):
  self.s.receive_profile(self.p,'p');self.s.receive_plan(self.q,'q');self.s.record_job('work-a','job-1','name')
  d=profile_dict();d['version']=2;d['meta']['request_id']='profile-v2';p=parse_profile(pb.PushProfileRequest(**d),NOW)
  with self.assertRaises(ContractError):self.s.receive_profile(p,'p-v2')
  self.assertEqual(self.s.get_execution('work-a').profile.version,1)
