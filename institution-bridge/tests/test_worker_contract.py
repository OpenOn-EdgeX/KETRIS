import importlib.util,unittest
from pathlib import Path
class Worker(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('worker',Path(__file__).parents[1]/'workload/worker.py');self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
 def test_identity_mismatch_refuses_benchmark(self):
  class Bench:
   calls=0
   def prepare(self):self.calls+=1;return 0
  b=Bench();events=[]
  with self.assertRaises(ValueError):self.w.execute(b,{'device_count':1,'actual_cuda_uuid':'other'},'expected',events.append)
  self.assertEqual(b.calls,0);self.assertFalse(events)
 def test_worker_no_fake_results_or_credit_mutation(self):
  class Bench:
   now=0;steps=0
   def prepare(self):return 0
   def step(self):self.now+=.2;self.steps+=1;return 0
  b=Bench();events=[];self.w.execute(b,{'device_count':1,'actual_cuda_uuid':'expected'},'expected',events.append,lambda:b.now,warmup=1,duration=2)
  self.assertEqual(events[-1]['event'],'complete');self.assertGreaterEqual(events[-1]['iterations'],10);self.assertEqual(b.steps,events[-1]['iterations']+5)
