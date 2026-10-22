import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from ketris_mig.contracts import AllocationBindingV1, resource_name, validate_request

MIG = 'MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf'
GPU = 'GPU-895722c8-b706-27ae-018d-c43e405e6c35'


class ContractTests(unittest.TestCase):
    def test_space_is_independent_of_time_weight(self):
        req = validate_request({'mig_uuid': MIG, 'space_percent': 50, 'time_weight': 3})
        self.assertEqual((req.space_percent, req.time_weight), (50, 3))
        self.assertEqual(resource_name(MIG), 'keti.re.kr/mig-4bf488e0e54112ad-share')

    def test_invalid_space_and_time_are_rejected(self):
        for field, bad in [('space_percent', x) for x in [True, float('nan'), float('inf'), 0, 101, 50.5, '50']] + [('time_weight', x) for x in [False, 0, 1001, float('nan'), 'nan']]:
            with self.subTest(field=field, value=bad):
                value = {'mig_uuid': MIG, 'space_percent': 50, 'time_weight': 3, field: bad}
                with self.assertRaises(ValueError):
                    validate_request(value)

    def test_unknown_fields_and_bad_uuid_do_not_silently_pass(self):
        for extra in [{'mig_uuid': '../fake'}, {'time_weigth': 3}]:
            with self.assertRaises(ValueError):
                validate_request({'mig_uuid': MIG, 'space_percent': 50, **extra})

    def test_binding_roundtrip_preserves_partition_and_container_identity(self):
        binding = AllocationBindingV1(allocation_id='a-test', node='gpu-npu-server-02', gpu_uuid=GPU,
            mig_uuid=MIG, generation='g1', resource_name=resource_name(MIG), device_ids=['u0', 'u1'],
            space_percent=2, control_dir='/run/ketris/tenant-a', pod_uid='pod-uid', container='main',
            time_weight=3, phase='bound')
        decoded = AllocationBindingV1.from_dict(binding.to_dict())
        self.assertEqual((decoded.gpu_uuid, decoded.mig_uuid, decoded.pod_uid, decoded.container), (GPU, MIG, 'pod-uid', 'main'))

    def test_binding_cannot_claim_a_different_partition_resource(self):
        with self.assertRaises(ValueError):
            AllocationBindingV1(allocation_id='a', node='node', gpu_uuid=GPU, mig_uuid=MIG, generation='g',
                resource_name='keti.re.kr/mig-fake-share', device_ids=['u0'], space_percent=1, control_dir='/tmp/a')


if __name__ == '__main__':
    unittest.main()
