import unittest
from ketris_mig.utilization_metrics import interval_mean, compare_percent
from ketris_mig.llm_experiment import summarize_requests, select_qualified

class UtilizationMetricsTest(unittest.TestCase):
    def test_no_slo_qualifier_has_explicit_absent_result(self):
        self.assertIsNone(select_qualified([{'eligible':False,'background_prompts_s':500}]))
    def test_arrival_latency_includes_delayed_enqueue(self):
        row={'release_ns':0,'enqueue_ns':20_000_000,'start_ns':20_000_000,'first_token_ns':30_000_000,'completion_ns':50_000_000,'generated_tokens':1}
        result=summarize_requests([row],25,45,arrival_basis='release_ns')
        self.assertEqual(result['ttft_ms']['p95'],30)
        self.assertEqual(result['completion_ms']['p95'],50)
        self.assertEqual(result['ttft_deadline_misses'],1)
    def test_clips_window_and_keeps_idle_time(self):
        r=interval_mean([(0,90),(10,0),(20,60),(30,0)],5,25,12)
        self.assertEqual(r['mean_percent'],30);self.assertEqual(r['coverage'],1)
    def test_missing_counter_support_is_not_idle(self):
        r=interval_mean([(0,0),(10,50),(30,100),(40,0)],0,40,12)
        self.assertEqual(r['coverage'],.5);self.assertEqual(r['mean_percent'],25)
    def test_invalid_or_empty_samples_not_a_zero_utilization_result(self):
        for rows in [[],[(0,0),(10,101)],[(0,0),(10,float('nan'))]]:
            with self.assertRaises(ValueError):interval_mean(rows,0,10,12)
    def test_percentage_points_and_relative_change_are_distinct(self):
        self.assertEqual(compare_percent(40,50),{'mps_percent':40,'ketris_percent':50,'difference_pp':10,'relative_change_percent':25})
        self.assertIsNone(compare_percent(0,50)['relative_change_percent'])
