import unittest
from ketris_mig.evidence import validate_measurement
class EvidenceTests(unittest.TestCase):
    def test_real_identity_counters_and_units_are_required(self):
        uuid='MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf'
        row={'config':'combined','repeat':1,'mig_uuid':uuid,'workers':{'A':{'actual_cuda_uuid':uuid,'reported_sm_count':22,'iterations':20,'elapsed_s':20,'charged_us':15000,'kernels':20}}}
        validate_measurement(row)
        for k,v in [('actual_cuda_uuid','GPU-other'),('kernels',0),('elapsed_s',0),('charged_us',0)]:
            old=row['workers']['A'][k];row['workers']['A'][k]=v
            with self.assertRaises(ValueError): validate_measurement(row)
            row['workers']['A'][k]=old
        row['mig_uuid']='MIG-placeholder';row['workers']['A']['actual_cuda_uuid']='MIG-placeholder'
        with self.assertRaises(ValueError):validate_measurement(row)
