import copy,unittest
from fixtures import *
from ketris_integration.contracts import *
from ketris_integration.rpc import institution_bridge_pb2 as pb
from ketris_integration.store import BridgeStore
import test_store
class JobsTests(unittest.TestCase):
 def setUp(self):
  from ketris_integration import jobs
  self.j=jobs;self.record=ExecutionRecord('work-a',parse_profile(pb.PushProfileRequest(**profile_dict()),NOW),parse_plan(pb.SubmitPartitionPlanRequest(**plan_dict()),NOW))
 def test_job_fixed_target_policy_and_template(self):
  j=self.j.build_job(self.record,'image:v1');p=j['spec']['template'];r=p['spec']['containers'][0]['resources']
  self.assertEqual(r['requests'][KEY],50);self.assertEqual(r['limits'][KEY],50);self.assertEqual(p['metadata']['annotations']['ketris.re.kr/time-weight'],'3')
 def test_no_node_name_and_no_unlimited_override(self):self.assertNotIn('nodeName',self.j.build_job(self.record,'image:v1')['spec']['template']['spec'])
 def test_gpu_vram_not_host_ram_request(self):self.assertEqual(self.j.build_job(self.record,'i')['spec']['template']['spec']['containers'][0]['resources']['requests']['memory'],'128Mi')
 def test_mutating_admission_defaults_do_not_change_contract(self):
  a=self.j.build_job(self.record,'i');b=copy.deepcopy(a);b['spec']['template']['metadata']['labels']['keti.re.kr/accel-class']='cpu';b['spec']['template']['spec']['containers'][0]['env'].append(dict(name='BLESS_TIME_WC',value='0'))
  self.assertEqual(self.j.project_execution_contract(a),self.j.project_execution_contract(b))

 def test_server_quantity_and_downward_api_defaults_preserve_contract(self):
  a=self.j.build_job(self.record,'i');b=copy.deepcopy(a);pod=b['spec']['template'];pod['spec']['nodeSelector']['keti.re.kr/node-role']='edge';c=pod['spec']['containers'][0]
  for side in ('requests','limits'):
   c['resources'][side]={k:str(v) for k,v in c['resources'][side].items()}
  c['env'][2]['valueFrom']['fieldRef']['apiVersion']='v1'
  self.assertEqual(self.j.project_execution_contract(a),self.j.project_execution_contract(b))
