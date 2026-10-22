import sys
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from ketris_mig.inventory import read_inventory, resolve_mig

GPU = 'GPU-895722c8-b706-27ae-018d-c43e405e6c35'
MIG = 'MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf'


def record(**changes):
    return {'gpu_uuid': GPU, 'gpu_index': 0, 'mig_uuid': MIG, 'gi_id': 1, 'ci_id': 0,
            'profile': '1g.24gb', 'memory_mib': 24186, 'profile_sm': 46, **changes}


class Adapter:
    def __init__(self, rows): self.rows = rows
    def scan(self): return self.rows


class InventoryTests(unittest.TestCase):
    def test_actual_mig_metadata_and_nullable_sm_are_preserved(self):
        values = read_inventory(Adapter([record(profile_sm=None)]))
        entry = resolve_mig(MIG, values)
        self.assertEqual((entry.gpu_uuid, entry.gi_id, entry.ci_id), (GPU, 1, 0))
        self.assertIsNone(entry.profile_sm)

    def test_changed_uuid_changes_partition_generation(self):
        a = read_inventory(Adapter([record()]))[0]
        b = read_inventory(Adapter([record(mig_uuid='MIG-00000000-0000-0000-0000-000000000002')]))[0]
        self.assertNotEqual(a.generation, b.generation)

    def test_multiple_compute_instances_of_one_gi_are_not_advertised(self):
        values = read_inventory(Adapter([record(), record(ci_id=1, mig_uuid='MIG-00000000-0000-0000-0000-000000000002')]))
        self.assertEqual(values, [])

    def test_unknown_partition_cannot_fall_back_to_parent_gpu(self):
        with self.assertRaises(ValueError): resolve_mig('MIG-00000000-0000-0000-0000-000000000002', read_inventory(Adapter([record()])))

    def test_duplicate_identity_and_driver_failure_are_errors(self):
        with self.assertRaises(ValueError): read_inventory(Adapter([record(), record()]))
        class Broken:
            def scan(self): raise RuntimeError('NVML unavailable')
        with self.assertRaises(RuntimeError): read_inventory(Broken())

    def test_hash_collision_cannot_bind_two_migs_to_one_resource(self):
        # A real 64-bit prefix collision is infeasible to construct in a unit test.
        class CollidingHash:
            def hexdigest(self): return '0' * 64
        rows = [record(), record(gi_id=2, mig_uuid='MIG-00000000-0000-0000-0000-000000000002')]
        with patch('hashlib.sha256', return_value=CollidingHash()):
            with self.assertRaises(ValueError): read_inventory(Adapter(rows))


if __name__ == '__main__': unittest.main()
