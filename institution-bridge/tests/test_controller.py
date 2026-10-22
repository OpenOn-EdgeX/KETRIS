import unittest,json
from types import SimpleNamespace
import test_admission
from ketris_integration.store import BridgeStore
class Controller(unittest.TestCase):
 def test_controller_orders_job_admission_native_ack_completion(self):
  from ketris_integration.controller import BridgeController
  # External Kube transport uses a literal complete Workload fixture; state logic stays real.
  case=test_admission.Admission();case.setUp()
  with __import__('tempfile').TemporaryDirectory() as d:
   store=BridgeStore(d+'/db');self.addCleanup(store.close);store.receive_profile(case.r.profile,'p');store.receive_plan(case.r.plan,'q')
   class Kube:
    def get_job(self,n):return case.j
    def list_workloads(self):return [case.w]
    def list_nodes(self):return case.nodes
    def get_flavor(self,n):return case.f
    def list_pods(self,u):return []
    def patch_check(self,w,p):case.w['status']['admissionChecks']+=p['status']['admissionChecks'];return case.w
   class Exports:
    def observe_target(self,t,n):return case.t
   settings=SimpleNamespace(image='ketris-institution-gemm:20261015-v1')
   c=BridgeController(store,Kube(),Exports(),settings);c.clock=lambda:__import__('fixtures').NOW;c.reconcile_once(__import__('fixtures').NOW)
   r=store.get_execution('work-a');self.assertEqual(r.state,'APPROVED');self.assertEqual(r.keti_decision_id,'decision-1');self.assertFalse(json.loads(r.evidence_json))
   store.transition('work-a','EXECUTION_OBSERVED',evidence_json="{\"kernels_delta\": 1, \"charged_delta_us\": 10, \"pod_uid\": \"pod-1\", \"container_id\": \"container-1\"}")
   case.j['status']={'conditions':[{'type':'Complete','status':'True'}]}
   case.w['status']['conditions']=[{'type':'Admitted','status':'False'},{'type':'Finished','status':'True'}]
   finished={'metadata':{'uid':'pod-1'},'status':{'containerStatuses':[{'name':'workload','containerID':'container-1','state':{'terminated':{'exitCode':0}}}]}}
   c.kube.list_pods=lambda uid:[finished]
   c.reconcile_once(__import__('fixtures').NOW)
   self.assertEqual(store.get_execution('work-a').state,'COMPLETED')
 def test_stale_runtime_export_does_not_invalidate_approved_execution(self):
  from ketris_integration.controller import BridgeController
  from ketris_integration.contracts import ContractError
  import tempfile,fixtures
  case=test_admission.Admission();case.setUp();case.w['status']['conditions'].append({'type':'Admitted','status':'True'})
  verdict=case.ev();case.w['status']['admissionChecks']+=case.a.own_check_patch(case.w,verdict)['status']['admissionChecks']
  with tempfile.TemporaryDirectory() as d:
   store=BridgeStore(d+'/db');self.addCleanup(store.close);store.receive_profile(case.r.profile,'p');store.receive_plan(case.r.plan,'q');store.record_job('work-a','job-1',case.j['metadata']['name']);store.record_admission('work-a',verdict.epoch,'decision-1');store.transition('work-a','EXECUTION_OBSERVED',evidence_json='{"kernels_delta":1,"charged_delta_us":10}')
   class K:
    def list_workloads(self):return [case.w]
    def get_job(self,n):return case.j
    def list_pods(self,u):return [{'status':{'containerStatuses':[{'name':'workload','state':{'running':{}}}]}}]
   class E:
    def observe_target(self,t,n):return case.t
    def read_domain(self,*args):raise ContractError('INVALID_ARGUMENT','stale partition export')
   c=BridgeController(store,K(),E(),type('Settings',(),{'image':'ketris-institution-gemm:20261015-v1'})());c.clock=lambda:fixtures.NOW;c.reconcile_once(fixtures.NOW)
   self.assertEqual(store.get_execution('work-a').state,'EXECUTION_OBSERVED')
 def test_export_freshness_uses_read_time_after_kubernetes_queries(self):
  from ketris_integration.controller import BridgeController
  from ketris_integration.runtime_evidence import fresh
  from ketris_integration.contracts import TargetObservation
  from datetime import timedelta
  import tempfile,fixtures
  case=test_admission.Admission();case.setUp();clock=[fixtures.NOW]
  with tempfile.TemporaryDirectory() as d:
   store=BridgeStore(d+'/db');self.addCleanup(store.close);store.receive_profile(case.r.profile,'p');store.receive_plan(case.r.plan,'q')
   class K:
    def list_workloads(self):clock[0]+=timedelta(seconds=2);return [case.w]
    def get_job(self,n):clock[0]+=timedelta(seconds=1);return case.j
    def list_nodes(self):return case.nodes
    def get_flavor(self,n):return case.f
    def list_pods(self,u):return []
    def patch_check(self,w,p):return w
   class E:
    def observe_target(self,t,n):
     at=n() if callable(n) else n
     return case.t if fresh({'observed_at':clock[0].isoformat()},at) else TargetObservation(False,reason='negative age')
   c=BridgeController(store,K(),E(),type('Settings',(),{'image':'ketris-institution-gemm:20261015-v1'})());c.clock=lambda:clock[0];c.reconcile_once(fixtures.NOW)
   self.assertEqual(store.get_execution('work-a').state,'APPROVED')
