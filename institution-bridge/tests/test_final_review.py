"""Regressions reproducing the final independent review findings."""
import copy,json,tempfile,unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import fixtures,test_admission
from ketris_integration.contracts import parse_profile,parse_plan,payload_digest
from ketris_integration.rpc import institution_bridge_pb2 as pb
from ketris_integration.store import BridgeStore
from ketris_integration.controller import BridgeController

class Review(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=BridgeStore(self.tmp.name+'/db');self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.close)
  self.case=test_admission.Admission();self.case.setUp();self.case.w['metadata']['resourceVersion']='10';self.created=[]
  case=self.case;created=self.created
  class Kube:
   def list_workloads(self):return [case.w]
   def get_job(self,n):return case.j
   def list_pods(self,u):return []
   def list_nodes(self):return case.nodes
   def get_flavor(self,n):return case.f
   def patch_check(self,w,p):
    case.w['status']['admissionChecks']=[a for a in case.w['status']['admissionChecks'] if a['name']!='ketris-plan-validation']+p['status']['admissionChecks']
   def create_job(self,j):created.append(copy.deepcopy(j));return {**j,'metadata':{**j['metadata'],'uid':'new-job'}}
  self.kube=Kube();self.exports=SimpleNamespace(observe_target=lambda *a:case.t)
  self.controller=BridgeController(self.store,self.kube,self.exports,SimpleNamespace(image='ketris-institution-gemm:20261015-v1'),clock=lambda:fixtures.NOW)
 def receive(self):
  self.store.receive_profile(self.case.r.profile,'p');self.store.receive_plan(self.case.r.plan,'q')
 def test_missing_referenced_version_waits_then_submits_exact_version(self):
  p=self.case.r.profile;d=fixtures.plan_dict();d['profile_version']=2;q=parse_plan(pb.SubmitPartitionPlanRequest(**d),fixtures.NOW)
  self.store.receive_profile(p,'p');self.store.receive_plan(q,'q');self.kube.list_workloads=lambda:[];jobs={}
  self.kube.get_job=lambda n:jobs.get(n)
  def create(j):self.created.append(copy.deepcopy(j));j=copy.deepcopy(j);j['metadata']['uid']='new-job';jobs[j['metadata']['name']]=j;return j
  self.kube.create_job=create
  self.controller.reconcile_once(fixtures.NOW)
  self.assertEqual(self.created,[],'unresolved v2 must not create a v1 Job')
  self.assertIsNone(self.store.get_execution('work-a').profile)
  d=fixtures.profile_dict();d['version']=2;d['meta']['request_id']='profile-v2';p2=parse_profile(pb.PushProfileRequest(**d),fixtures.NOW);self.store.receive_profile(p2,'p2')
  self.controller.reconcile_once(fixtures.NOW);r=self.store.get_execution('work-a');self.assertEqual(len(self.created),1);self.assertEqual(r.profile.version,2);self.assertTrue(r.job_uid)
 def test_ready_patch_carries_observed_resource_version(self):
  a=self.case.a;p=a.own_check_patch(self.case.w,self.case.ev());self.assertEqual(p['metadata'].get('resourceVersion'),'10')
 def test_conflicting_ready_write_retries_new_decision_from_fresh_read(self):
  self.receive();attempts=[]
  def patch_check(w,p):
   attempts.append(copy.deepcopy(p))
   if len(attempts)==1:
    self.case.w['metadata']['resourceVersion']='11';self.case.w['status']['admissionChecks'][0]['podSetUpdates'][0]['labels']['keti.re.kr/decision-id']='decision-2'
    raise RuntimeError('409 Conflict')
   self.assertEqual(p['metadata'].get('resourceVersion'),'11')
  self.kube.patch_check=patch_check;self.controller.reconcile_once(fixtures.NOW);self.controller.reconcile_once(fixtures.NOW)
  self.assertEqual(len(attempts),2);self.assertEqual(self.store.get_execution('work-a').keti_decision_id,'decision-2')
 def test_admitted_state_cannot_reuse_stale_epoch_or_own_check_message(self):
  for mutate in ('decision','message','reservation'):
   with self.subTest(mutate=mutate):
    case=self.case;self.receive();self.store.record_job('work-a','job-1',case.j['metadata']['name']);verdict=case.ev();self.store.record_admission('work-a',verdict.epoch,verdict.keti_decision_id)
    own=case.a.own_check_patch(case.w,verdict)['status']['admissionChecks'][0];case.w['status']['admissionChecks']=[case.w['status']['admissionChecks'][0],own];case.w['status']['conditions'].append({'type':'Admitted','status':'True'})
    if mutate=='decision':case.w['status']['admissionChecks'][0]['podSetUpdates'][0].update(labels={'keti.re.kr/decision-id':'decision-2'},nodeSelector={'kubernetes.io/hostname':'other'})
    elif mutate=='message':own['message']='{"epoch":"stale","decision":"stale"}'
    else:case.w['status']['conditions'][0]['lastTransitionTime']='new-reservation'
    self.controller.reconcile_once(fixtures.NOW)
    self.assertEqual(self.store.get_execution('work-a').state,'REPLAN_REQUIRED')
    self.assertEqual(next(a for a in case.w['status']['admissionChecks'] if a['name']=='ketris-plan-validation')['state'],'Rejected')
    if mutate!='reservation':
     self.store.close();self.store=BridgeStore(self.tmp.name+'/db-'+mutate);self.addCleanup(self.store.close);self.controller.store=self.store;case.setUp();case.w['metadata']['resourceVersion']='10'
