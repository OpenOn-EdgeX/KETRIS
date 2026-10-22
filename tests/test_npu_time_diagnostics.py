"""Instrumentation tests; no accelerator or inference is involved."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime/npu_proxy'))
import gate_core
from diagnostics import DiagnosticGate, summarize_window, window_drained


class DiagnosticTests(unittest.TestCase):
    def test_http_response_does_not_mean_accounting_has_drained(self):
        self.assertFalse(window_drained({'A':dict(queue=0, diag_inflight=1)}))
        self.assertFalse(window_drained({'A':dict(queue=1, diag_inflight=0)}))
        self.assertTrue(window_drained({'A':dict(queue=0, diag_inflight=0)}))
        self.assertFalse(window_drained({}))
    def test_window_keeps_nominal_accepted_contract_and_extra_separate(self):
        before = {n:dict(diag_requested_us=0, diag_clipped_us=0, granted_us=0,
                        charged_us=0, charged_extra_us=0, done=0, done_extra=0) for n in ['A', 'B']}
        after = {'A':dict(diag_requested_us=700,diag_clipped_us=200,granted_us=700,
                         charged_us=400,charged_extra_us=100,done=5,done_extra=1),
                 'B':dict(diag_requested_us=300,diag_clipped_us=0,granted_us=300,
                         charged_us=300,charged_extra_us=100,done=4,done_extra=1)}
        result = summarize_window(before, after)
        self.assertAlmostEqual(result['A']['requested_share'], .7)
        self.assertAlmostEqual(result['A']['accepted_share'], 500/800)
        self.assertAlmostEqual(result['A']['denominator_share'], .7)
        self.assertAlmostEqual(result['A']['contract_share'], 400/700)
        self.assertAlmostEqual(result['A']['total_accounted_share'], 500/900)
        self.assertEqual(result['A']['clipped_us'], 200)

    def test_observer_reports_requested_accepted_and_clipped_without_changing_core(self):
        with patch.object(gate_core, 'CREDIT_CAP_US', 1_000_000):
            g = DiagnosticGate(lambda r: None)
            plain = gate_core.GateCore(lambda r: None)
            for core in (g, plain):
                for name in ('A', 'B'):
                    core.register(name); core.cmd(name, 'time_mode', '1')
                for _ in range(10):
                    core.cmd('A', 'time_add', '210000')
                    core.cmd('B', 'time_add', '90000')
            for name in ('A', 'B'):
                for key, value in plain.stats()[name].items():
                    self.assertEqual(g.stats()[name][key], value)
            self.assertEqual(g.stats()['A']['diag_requested_us'], 2_100_000)
            self.assertEqual(g.stats()['A']['diag_clipped_us'], 1_100_000)
            self.assertEqual(g.stats()['B']['diag_clipped_us'], 0)

    def test_denominator_control_changes_only_granted_and_keeps_real_credit_cap(self):
        with patch.object(gate_core, 'CREDIT_CAP_US', 1_000_000):
            g = DiagnosticGate(lambda r: None, denominator='nominal')
            g.register('A'); g.cmd('A', 'time_mode', '1')
            for _ in range(10):
                g.cmd('A', 'time_add', '210000')
            self.assertEqual(g.stats()['A']['granted_us'], 2_100_000)
            self.assertEqual(g.stats()['A']['credit_us'], 1_000_000)
            self.assertEqual(g.stats()['A']['diag_clipped_us'], 1_100_000)
            self.assertEqual(g.stats()['A']['charged_extra_us'], 0)

    def test_inflight_decision_and_completion_are_observed_on_actual_step(self):
        clock = [0.0]
        seen = []
        def execute(req):
            seen.append(g.stats()['A']['diag_inflight'])
            clock[0] += .2
        g = DiagnosticGate(execute, clock=lambda: clock[0])
        g.register('A'); g.cmd('A', 'time_mode', '1'); g.submit('A', 'actual req')
        self.assertEqual(g.step()[0], 'A')  # WC execution at zero credit.
        self.assertEqual(seen, [1])
        self.assertEqual(g.stats()['A']['diag_inflight'], 0)
        self.assertEqual(g.stats()['A']['charged_extra_us'], 200_000)
        events = g.drain_events()
        self.assertEqual([e['kind'] for e in events], ['selected', 'completed'])
        self.assertEqual(g.drain_events(), [])


if __name__ == '__main__':
    unittest.main()
