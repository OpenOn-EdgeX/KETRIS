import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from dataclasses import replace
from fixtures import *
from ketris_integration.contracts import *
from ketris_integration.rpc import institution_bridge_pb2 as pb
D=Path(__file__).parent/'data'
class Runtime(unittest.TestCase):
 def setUp(self):
  from ketris_integration import runtime_evidence as rt
  self.rt=rt;self.part=json.loads((D/'partition.json').read_text());self.bind=json.loads((D/'bindings.json').read_text());self.pod=json.loads((D/'pod-a.json').read_text());self.now=datetime.fromisoformat(self.part['observed_at'])
  d=plan_dict();d['target']['inventory_generation']=self.part['generation'];self.q=parse_plan(pb.SubmitPartitionPlanRequest(**d),NOW);self.r=ExecutionRecord('work-a',parse_profile(pb.PushProfileRequest(**profile_dict()),NOW),self.q,job_uid=self.pod['metadata']['ownerReferences'][0]['uid'],digest='digest1');self.job={'metadata':{'uid':self.r.job_uid}};self.pod['metadata']['annotations']['ketris.integration/config-digest']='digest1'
 def assess(self,previous=None):return self.rt.assess_apply(self.r,self.job,self.pod,DomainSnapshot(canonical(self.part),canonical(list(self.bind.values())),self.part['observed_at']),previous,self.now)
 def test_bound_and_armed_real_uid_container_cuda_required(self):
  a=self.assess();self.assertEqual(a.state,'APPLIED_VERIFIED');self.bind['A']['actual_cuda_uuid']='wrong';self.assertNotEqual(self.assess().state,'APPLIED_VERIFIED')
 def test_fresh_timestamp_stagnant_counters_not_execution(self):
  a=self.assess();self.now+=timedelta(seconds=1);self.part['observed_at']=self.now.isoformat();self.assertEqual(self.assess(a.sample_json).state,'APPLIED_VERIFIED')
  self.part['tenants'][0]['charged_total_us']+=100;self.part['tenants'][0]['kernels']+=1;self.assertEqual(self.assess(a.sample_json).state,'EXECUTION_OBSERVED')
 def test_new_runtime_generation_resets_baseline(self):
  a=self.assess();self.part['tenants'][0]['runtime_generation']='new';self.bind['A']['runtime_generation']='new';self.assertNotEqual(self.assess(a.sample_json).state,'EXECUTION_OBSERVED')
 def test_other_pod_same_name_not_joined(self):
  self.pod['metadata']['uid']='other';self.assertNotEqual(self.assess().state,'APPLIED_VERIFIED')
 def test_single_peer_budget_not_false_failure(self):
  self.part['tenants']=self.part['tenants'][:1];self.part['tenants'][0]['budget_us']=10000;self.assertEqual(self.assess().state,'APPLIED_VERIFIED')
 def test_extension_fields_accepted_without_following_socket_path(self):
  self.bind['A']['socket']='/forbidden/control.sock';self.assertEqual(self.assess().state,'APPLIED_VERIFIED')
 def test_stale_partial_or_replaced_files_unavailable(self):
  self.now+=timedelta(seconds=6);self.assertNotEqual(self.assess().state,'APPLIED_VERIFIED')
  with tempfile.TemporaryDirectory() as d:
   b=Path(d)/'bindings';p=Path(d)/'partitions';b.mkdir();p.mkdir();(p/'x.json').symlink_to(D/'partition.json');reader=self.rt.ExportReader(b,p)
   self.assertFalse(reader.observe_target(self.q.target,self.now).available)
