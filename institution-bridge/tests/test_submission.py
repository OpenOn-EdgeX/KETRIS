import unittest
from ketris_integration.contracts import ContractError
import test_store
class Submission(unittest.TestCase):
 setUp=test_store.StoreTests.setUp
 tearDown=test_store.StoreTests.tearDown
 def test_adopt_only_exact_matching_owned_job(self):
  from ketris_integration.jobs import ensure_job
  self.s.receive_profile(self.p,'p');self.s.receive_plan(self.q,'q');r=self.s.get_execution('work-a');k=FakeKube()
  j=ensure_job(r,self.s,k,'image:v1');j['spec']['template']['spec']['containers'][0]['image']='evil'
  with self.assertRaises(ContractError):ensure_job(self.s.get_execution('work-a'),self.s,k,'image:v1')
 def test_create_then_crash_retry_same_job_uid(self):
  from ketris_integration.jobs import ensure_job
  self.s.receive_profile(self.p,'p');self.s.receive_plan(self.q,'q');r=self.s.get_execution('work-a');k=FakeKube()
  first=ensure_job(r,self.s,k,'image:v1');second=ensure_job(r,self.s,k,'image:v1');self.assertEqual(first['metadata']['uid'],second['metadata']['uid']);self.assertEqual(k.creates,1)
class FakeKube:
 def __init__(self):self.job=None;self.creates=0
 def get_job(self,name):return self.job
 def create_job(self,j):self.creates+=1;j['metadata']['uid']='job-uid-1';self.job=j;return j
