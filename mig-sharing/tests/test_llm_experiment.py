import unittest
import importlib.util
from pathlib import Path
import tempfile

from ketris_mig.llm_experiment import clean_native_env, summarize_requests, summarize_batches, cas_node_catalog, wait_until


class LlmMeasurementTests(unittest.TestCase):
    def row(self, first=30_000_000, done=80_000_000):
        return dict(release_ns=0, enqueue_ns=10_000_000, start_ns=20_000_000,
                    first_token_ns=first, completion_ns=done, generated_tokens=4)

    def test_ttft_uses_completed_first_token_and_keeps_enqueue_lag(self):
        value = summarize_requests([self.row()], ttft_deadline_ms=15, completion_deadline_ms=100)
        self.assertEqual(value['ttft_ms']['p95'], 20)
        self.assertEqual(value['queue_ms']['p95'], 10)
        self.assertEqual(value['enqueue_lag_ms']['p95'], 10)
        self.assertEqual(value['completion_ms']['p95'], 70)
        self.assertEqual(value['ttft_deadline_misses'], 1)
        self.assertEqual(value['completion_deadline_misses'], 0)

    def test_invalid_timestamps_and_empty_successes_cannot_be_summarized(self):
        for rows in ([], [self.row(first=15_000_000)], [dict(self.row(), generated_tokens=0)]):
            with self.assertRaises(ValueError):
                summarize_requests(rows, 50, 100)

    def test_fixed_window_excludes_warmup_and_straddling_batches(self):
        rows = [dict(start_ns=5, end_ns=15, prompts=4, input_tokens=40),
                dict(start_ns=20, end_ns=30, prompts=4, input_tokens=40),
                dict(start_ns=90, end_ns=110, prompts=4, input_tokens=40)]
        result = summarize_batches(rows, 10, 100)
        self.assertEqual(result['completed_batches'], 1)
        self.assertEqual(result['completed_prompts'], 4)
        self.assertEqual(result['boundary_batches_excluded'], 2)

    def test_native_cuda_worker_has_no_shim_policy_but_preserves_mps(self):
        env = dict(LD_PRELOAD='/ketris/libbless.so', BLESS_TIME_START_GATED='1',
                   KETRIS_TIME_WEIGHT='3', CUDA_MPS_ACTIVE_THREAD_PERCENTAGE='50',
                   CUDA_MPS_CLIENT_PRIORITY='1', CUDA_VISIBLE_DEVICES='MIG-real', LD_LIBRARY_PATH='/ketris/cuda-lib')
        clean = clean_native_env(env)
        self.assertNotIn('LD_PRELOAD', clean)
        self.assertNotIn('BLESS_TIME_START_GATED', clean)
        self.assertEqual(clean['CUDA_MPS_ACTIVE_THREAD_PERCENTAGE'], '50')
        self.assertEqual(clean['CUDA_MPS_CLIENT_PRIORITY'], '1')
        self.assertEqual(env['LD_PRELOAD'], '/ketris/libbless.so')

    def test_catalog_restore_preserves_other_nodes_and_rejects_changed_owner(self):
        old = {'status': 'old'}; mine = {'status': 'mine'}
        current = {'nodes': {'a': mine, 'b': {'status': 'new external'}}}
        restored = cas_node_catalog(current, 'a', mine, old)
        self.assertEqual(restored['nodes']['b'], current['nodes']['b'])
        self.assertEqual(restored['nodes']['a'], old)
        with self.assertRaises(RuntimeError):
            cas_node_catalog(current, 'a', old, mine)

    def test_arrival_deadline_crossing_does_not_sleep_negative(self):
        readings = iter([99, 101])
        sleeps = []
        wait_until(100, clock=lambda: next(readings), sleeper=sleeps.append)
        self.assertEqual(sleeps, [1e-9])


class OwnershipTests(unittest.TestCase):
    def make(self, directory):
        source = Path(__file__).resolve().parents[1] / 'tools/verify_llm.py'
        spec = importlib.util.spec_from_file_location('llm_lab_harness', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lab = module.Experiment(Path(directory) / 'owned-test', 'test-image', 1)
        lab.calls = []
        def command(args, **kw):
            lab.calls.append(args)
            return 'GPU idle'
        lab.cmd = command
        lab.get = lambda *a, **kw: {}
        return lab

    def test_preflight_failure_does_not_delete_namespace_or_restart_mps(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.make(directory)
            lab.baseline_active = True  # An observation is not a completed stop.
            lab.cleanup()
            self.assertFalse(any('delete' in call or 'systemctl' in call for call in lab.calls))

    def test_replaced_namespace_prevents_gpu_teardown(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.make(directory)
            lab.namespace_uid = 'original'
            lab.get = lambda *a, **kw: {'metadata': {'uid': 'replacement'}}
            lab.mig_enabled = True
            lab.baseline_stopped = True
            lab.gpu_uuid = 'GPU-test'
            with self.assertRaisesRegex(RuntimeError, 'Restoration needs inspection'):
                lab.cleanup()
            self.assertFalse(any('-mig' in call or 'systemctl' in call for call in lab.calls))

    def test_unavailable_namespace_lookup_is_not_assumed_absent(self):
        from unittest.mock import patch
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            lab = self.make(directory)
            with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'API unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'namespace lookup unavailable'):
                    lab.get_namespace_exists()


if __name__ == '__main__':
    unittest.main()
