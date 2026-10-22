import importlib.util,tempfile,unittest,json,sys
from pathlib import Path
class ResultTests(unittest.TestCase):
 def setUp(self):
  s=importlib.util.spec_from_file_location('verify_result',Path(__file__).parents[1]/'tools/verify_result.py');self.m=importlib.util.module_from_spec(s);sys.modules[s.name]=self.m;s.loader.exec_module(self.m)
 def test_receipt_or_node_only_result_not_pass(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'run-info.json').write_text('{"rpc_ok":true,"node":"devmaster"}')
   r=self.m.verify_result(p);self.assertFalse(r.passed);self.assertFalse(r.checks['execution_progress'])
 def test_uid_digest_or_epoch_mismatch_rejected(self):
  self.assertFalse(self.m.link_matches({'job_uid':'one','config_digest':'d','admission_epoch':'e'},{'job_uid':'two','approved_config_digest':'d','admission_epoch':'e'}))
 def test_same_generation_counter_deltas_and_same_domain_share_required(self):
  start={'observed_at':'2026-10-15T00:00:00+00:00','generation':'gen','tenants':[{'allocation_id':'a','pod_uid':'pa','container_id':'ca','runtime_generation':'ga','kernels':10,'charged_total_us':100},{'allocation_id':'b','pod_uid':'pb','container_id':'cb','runtime_generation':'gb','kernels':5,'charged_total_us':50}]}
  end=json.loads(json.dumps(start));end['observed_at']='2026-10-15T00:00:20+00:00';end['tenants'][0]['runtime_generation']='new'
  self.assertRaises(ValueError,self.m.measure_window,start,end,['pa','pb'])
 def test_live_dashboard_snapshot_must_match_executing_pod_uids(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'dashboard-active.json').write_text(json.dumps({'captured_at':'2026-10-15T00:00:10+00:00','partitions':[{'kind':'mig','metrics':{'mig_uuid':{'value':'mig-a'}}},{'kind':'mig','metrics':{'mig_uuid':{'value':'mig-b'}}}],'workloads':[{'pod_uid':'unrelated'}]}))
   (p/'target.json').write_text(json.dumps({'mig_uuid':'mig-a'}));(p/'states-executing.json').write_text(json.dumps([{'applied':{'pod_uid':'pa'}},{'applied':{'pod_uid':'pb'}}]));r=self.m.verify_result(p)
   self.assertFalse(r.checks['live_dashboard'])
