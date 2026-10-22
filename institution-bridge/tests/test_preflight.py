import unittest,importlib.util
from pathlib import Path
class Preflight(unittest.TestCase):
 def test_unscheduled_native_job_does_not_count_as_active_gpu(self):
  s=importlib.util.spec_from_file_location('preflight',Path(__file__).parents[1]/'tools/preflight.py');m=importlib.util.module_from_spec(s);__import__('sys').modules[s.name]=m;s.loader.exec_module(m)
  class K:
   def get(self,*args):
    if args==('nodes',):return {'items':[{'metadata':{'name':'devmaster'},'status':{'allocatable':{'keti.re.kr/mig-test-share':'200'}}}]}
    if args==('pods','-A'):return {'items':[{'metadata':{'namespace':'other','name':'queued'},'status':{'phase':'Pending'},'spec':{'containers':[{'resources':{'requests':{'keti.re.kr/lsu':'240'}}}]}}]}
    return {'spec':{}}
   def partition(self,key):return {'manager_state':'running','tenants':[],'inventory':{'gpu_uuid':'g','mig_uuid':'m','resource_name':key,'generation':'gen'}}
  r=m.preflight(K());self.assertTrue(r.ok);self.assertEqual(r.active_gpu_pods,[])
