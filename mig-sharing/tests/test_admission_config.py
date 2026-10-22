import unittest
from ketris_mig.admission import mig_admission_manifest
class AdmissionConfigTests(unittest.TestCase):
    def test_mig_only_fail_closed_create_update_limits_and_requests(self):
        legacy={'webhooks':[{'clientConfig':{'service':{'name':'ketris-webhook','namespace':'kube-system','path':'/mutate','port':443},'caBundle':'public-ca'},'namespaceSelector':{},'admissionReviewVersions':['v1'],'sideEffects':'None'}]}
        wh=mig_admission_manifest(legacy)['webhooks'][0]
        self.assertEqual(wh['failurePolicy'],'Fail');self.assertNotIn('namespaceSelector',wh)
        self.assertIn('UPDATE',wh['rules'][0]['operations'])
        expression=wh['matchConditions'][0]['expression'];self.assertIn('c.resources.requests',expression);self.assertIn('c.resources.limits',expression)
        self.assertEqual(wh['clientConfig'],legacy['webhooks'][0]['clientConfig'])
