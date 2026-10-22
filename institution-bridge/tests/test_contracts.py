import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fixtures import *
class Contracts(unittest.TestCase):
 def setUp(self):
  from ketris_integration import contracts as c
  from ketris_integration.rpc import institution_bridge_pb2 as pb
  self.c,self.pb=c,pb
 def profile(self,d=None): return self.pb.PushProfileRequest(**(d or profile_dict()))
 def plan(self,d=None): return self.pb.SubmitPartitionPlanRequest(**(d or plan_dict()))
 def test_live_profile_units_and_provenance(self):
  p=self.c.parse_profile(self.profile(),NOW)
  self.assertEqual(p.gpu_memory_bytes,1073741824);self.assertEqual(p.origin,'SYNTHETIC');self.assertTrue(p.executable)
 def test_unknown_units_not_executable(self):
  d=profile_dict();d['gpu_memory_unit']='unknown'
  with self.assertRaises(self.c.ContractError): self.c.parse_profile(self.profile(d),NOW)
 def test_contract_only_cannot_start_job(self):
  d=profile_dict();d['meta']['execution_mode']='CONTRACT_ONLY'
  self.assertFalse(self.c.parse_profile(self.profile(d),NOW).executable)
 def test_target_hash_generation_and_integer_policy(self):
  self.assertEqual(self.c.mig_resource_name(UUID),KEY)
  self.assertEqual(self.c.parse_plan(self.plan(),NOW).space_percent,50)
  for key,value in [('space_percent',0),('space_percent',101),('time_weight',0),('time_weight',1001)]:
   d=plan_dict();d[key]=value
   with self.assertRaises(self.c.ContractError): self.c.parse_plan(self.plan(d),NOW)
  d=plan_dict();d['target']['resource_name']='keti.re.kr/wrong'
  with self.assertRaises(self.c.ContractError): self.c.parse_plan(self.plan(d),NOW)
 def test_expired_and_nonfinite_predictions(self):
  d=profile_dict();d['meta']['expires_at_unix_s']=1
  with self.assertRaises(self.c.ContractError): self.c.parse_profile(self.profile(d),NOW)
  for n in [float('nan'),float('inf'),-1]:
   d=profile_dict();d['predicted_latency_ms']=n
   with self.assertRaises(self.c.ContractError): self.c.parse_profile(self.profile(d),NOW)
 def test_semantic_digest_ignores_receipt_time_but_not_policy(self):
  p=self.c.parse_profile(self.profile(),NOW);a=self.c.parse_plan(self.plan(),NOW)
  d=plan_dict();d['space_percent']=25;b=self.c.parse_plan(self.plan(d),NOW)
  self.assertNotEqual(self.c.config_digest(p,a),self.c.config_digest(p,b))
  self.assertEqual(self.c.payload_digest(self.profile()),self.c.payload_digest(self.profile()))
