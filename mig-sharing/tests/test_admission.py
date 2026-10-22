import copy
import importlib.util
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).parents[2]
sys.path.insert(0,str(ROOT/'mig-sharing'))
from ketris_mig.admission import validate_pod
from ketris_mig.contracts import ShareRequest
from ketris_mig.inventory import MigInventoryEntry
from ketris_mig.manifest import build_manifest

MIG='MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf'
ENTRY=MigInventoryEntry('GPU-895722c8-b706-27ae-018d-c43e405e6c35',0,MIG,1,0,'1g.24gb',24186,46,'g1')
RES='keti.re.kr/mig-4bf488e0e54112ad-share'


class AdmissionTests(unittest.TestCase):
    def pod(self):
        return build_manifest(ShareRequest(MIG,50,3),'ketris-device-plugin:exp158','test',['python3','/bench.py'],[ENTRY])

    def test_resource_selects_exact_mig_and_weight_does_not_change_space(self):
        pod=self.pod();result=validate_pod(pod,[ENTRY])
        c=pod['spec']['containers'][0]
        self.assertEqual(c['resources']['limits'][RES],50)
        self.assertEqual(c['resources']['requests'][RES],50)
        self.assertEqual(result.contracts['workload'].time_weight,3)

    def test_missing_partition_or_mismatched_annotation_is_rejected(self):
        with self.assertRaises(ValueError):validate_pod(self.pod(),[])
        pod=self.pod();pod['metadata']['annotations']['ketris.re.kr/mig-uuid']='MIG-00000000-0000-0000-0000-000000000002'
        with self.assertRaises(ValueError):validate_pod(pod,[ENTRY])

    def test_protected_env_override_is_rejected(self):
        for name,value in [('LD_PRELOAD',''),('CUDA_VISIBLE_DEVICES','0'),('CUDA_MPS_ACTIVE_THREAD_PERCENTAGE','100'),('BLESS_TIME_WC','1'),('KETRIS_TIME_WEIGHT','nan')]:
            pod=self.pod();pod['spec']['containers'][0]['env'].append({'name':name,'value':value})
            with self.subTest(name=name),self.assertRaises(ValueError):validate_pod(pod,[ENTRY])

    def test_resource_mismatch_mixed_gpu_and_unsupported_init_are_rejected(self):
        for kind in ['count','lsu','init']:
            pod=self.pod();c=pod['spec']['containers'][0]
            if kind=='count':c['resources']['requests'][RES]=49
            elif kind=='lsu':c['resources']['limits']['keti.re.kr/lsu']=10
            else:pod['spec']['initContainers']=[copy.deepcopy(c)]
            with self.subTest(kind=kind),self.assertRaises(ValueError):validate_pod(pod,[ENTRY])

    def test_non_mig_workload_is_untouched(self):
        self.assertEqual(validate_pod({'spec':{'containers':[{'name':'cpu'}]}},[]).patches,[])

    def test_ambiguous_catalog_is_rejected(self):
        with self.assertRaises(ValueError): validate_pod(self.pod(),[ENTRY,ENTRY])

    def test_unknown_envfrom_cannot_override_policy(self):
        pod=self.pod();pod['spec']['containers'][0]['envFrom']=[{'configMapRef':{'name':'arbitrary'}}]
        with self.assertRaises(ValueError): validate_pod(pod,[ENTRY])

    def test_actual_webhook_fails_closed_for_bad_mig(self):
        spec=importlib.util.spec_from_file_location('ketris_webhook',ROOT/'webhook/webhook_server.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        pod=self.pod();pod['metadata']['annotations']['ketris.re.kr/time-weight']='nan'
        reply=module.admission_response({'uid':'test-uid','object':pod},mig_inventory=[ENTRY])
        self.assertFalse(reply['allowed'])
        self.assertEqual(reply['uid'],'test-uid')


if __name__=='__main__':unittest.main()
