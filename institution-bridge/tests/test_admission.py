import unittest,copy
from dataclasses import replace
from fixtures import *
from ketris_integration.contracts import *
from ketris_integration.rpc import institution_bridge_pb2 as pb
from ketris_integration.jobs import build_job
class Admission(unittest.TestCase):
 def setUp(self):
  from ketris_integration import admission
  self.a=admission;p=parse_profile(pb.PushProfileRequest(**profile_dict()),NOW);q=parse_plan(pb.SubmitPartitionPlanRequest(**plan_dict()),NOW)
  self.r=ExecutionRecord('work-a',p,q,job_uid='job-1');self.j=build_job(self.r,'ketris-institution-gemm:20261015-v1');self.j['metadata']['uid']='job-1'
  self.w=dict(apiVersion='kueue.x-k8s.io/v1beta2',kind='Workload',metadata=dict(name='wl-1',uid='wl-uid',resourceVersion='1',namespace='ketris-integration-workloads',ownerReferences=[dict(kind='Job',uid='job-1')]),spec=dict(podSets=[dict(name='main',count=1,template=copy.deepcopy(self.j['spec']['template']))]),status=dict(conditions=[dict(type='QuotaReserved',status='True',lastTransitionTime='t1')],admission=dict(podSetAssignments=[dict(name='main',flavors={KEY:'ketris-integration-devmaster','cpu':'ketris-integration-devmaster','memory':'ketris-integration-devmaster'},resourceUsage={KEY:'50','cpu':'200m','memory':'128Mi'},count=1)]),admissionChecks=[dict(name='keti-scheduler',state='Ready',podSetUpdates=[dict(name='main',labels={'keti.re.kr/decision-id':'decision-1'},nodeSelector={'kubernetes.io/hostname':'devmaster'})])]))
  self.nodes=[dict(metadata=dict(name='devmaster',labels={'kubernetes.io/hostname':'devmaster'}),status=dict(allocatable={KEY:'200'}))];self.f=dict(metadata=dict(name='ketris-integration-devmaster'),spec=dict(nodeLabels={'kubernetes.io/hostname':'devmaster'}));self.t=TargetObservation(True,'generation-1',inventory_json=canonical(dict(gpu_uuid=self.r.plan.target.gpu_uuid,memory_mib=24192)),manager_state='running')
 def ev(self):return self.a.evaluate_admission(self.r,self.j,self.w,self.nodes,self.f,self.t,NOW)
 def test_waits_for_quota_and_existing_keti_ready(self):
  self.w['status']['conditions']=[];self.assertEqual(self.ev().state,'Pending')
  self.w['status']['conditions']=[dict(type='QuotaReserved',status='True')];self.w['status']['admissionChecks'][0]['state']='Pending';self.assertEqual(self.ev().state,'Pending')
 def test_only_exact_profile_plan_target_and_policy_ready(self):self.assertEqual(self.ev().state,'Ready')
 def test_existing_keti_other_node_rejected(self):
  self.w['status']['admissionChecks'][0]['podSetUpdates'][0]['nodeSelector']['kubernetes.io/hostname']='other';self.assertEqual(self.ev().state,'Rejected')
 def test_expiry_generation_and_flavor_changes_rejected(self):
  self.t=replace(self.t,generation='changed');self.assertEqual(self.ev().state,'Rejected')
 def test_eviction_or_new_decision_invalidates_prior_epoch(self):
  first=self.ev().epoch;self.w['status']['admissionChecks'][0]['podSetUpdates'][0]['labels']['keti.re.kr/decision-id']='decision-2';self.assertNotEqual(self.ev().epoch,first)
  self.w['status']['conditions'].append(dict(type='Evicted',status='True'));self.assertEqual(self.ev().state,'Pending')
 def test_patch_preserves_other_check_and_selector(self):
  old=copy.deepcopy(self.w);p=self.a.own_check_patch(self.w,self.ev());self.assertEqual(self.w,old);self.assertEqual([x['name'] for x in p['status']['admissionChecks']],['ketris-plan-validation']);self.assertNotIn('podSetUpdates',p['status']['admissionChecks'][0])
