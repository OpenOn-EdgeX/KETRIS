import unittest,importlib.util
from pathlib import Path
from ketris_integration.contracts import LiveMigTarget,ContractError
from fixtures import UUID,KEY
class Installation(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('render',Path(__file__).parents[1]/'deploy/render.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
  self.docs=self.m.render_installation(LiveMigTarget('devmaster','GPU-4a42bf67-df51-7582-9e81-b9dddd4bcea2',UUID,KEY,'gen'),dict(bridge='bridge',worker='worker'))
 def get(self,k,n):return next(x for x in self.docs if x['kind']==k and x['metadata']['name']==n)
 def test_control_no_router_optin_and_workloads_explicit_queue(self):
  self.assertNotIn('keti.re.kr/exec-router',self.get('Namespace','ketris-integration')['metadata']['labels']);self.assertEqual(self.get('Namespace','ketris-integration-workloads')['metadata']['labels']['keti.re.kr/exec-router'],'enabled')
 def test_both_admission_checks_and_no_cohort(self):
  cq=self.get('ClusterQueue','ketris-integration-mig');self.assertNotIn('cohort',cq['spec']);self.assertEqual([x['name'] for x in cq['spec']['admissionChecksStrategy']['admissionChecks']],['keti-scheduler','ketris-plan-validation'])
 def test_rbac_cannot_write_shared_resources(self):
  rules=self.get('ClusterRole','ketris-institution-bridge')['rules']
  for r in rules:
   if set(r['verbs'])-{'get','list','watch'}:self.assertEqual(r.get('resourceNames'),['ketris-plan-validation']);self.assertEqual(r['resources'],['admissionchecks/status'])
 def test_only_two_runtime_ro_directory_mounts(self):
  spec=self.get('Deployment','ketris-institution-bridge')['spec']['template']['spec'];hp={v['name'] for v in spec['volumes'] if 'hostPath' in v};mounts=[m for m in spec['containers'][0]['volumeMounts'] if m['name'] in hp]
  self.assertEqual({m['mountPath'] for m in mounts},{'/exports/bindings','/exports/partitions'});self.assertTrue(all(m['readOnly'] for m in mounts))
 def test_root_read_permissions_without_privileged_or_sockets(self):
  c=self.get('Deployment','ketris-institution-bridge')['spec']['template']['spec']['containers'][0];s=c['securityContext'];self.assertEqual(s['runAsUser'],0);self.assertFalse(s['privileged']);self.assertFalse(s['allowPrivilegeEscalation']);self.assertTrue(s['readOnlyRootFilesystem'])
 def test_deploy_refuses_unowned_existing_names(self):
  self.assertRaises(ContractError,self.m.check_owned,{'metadata':{'labels':{}}})
