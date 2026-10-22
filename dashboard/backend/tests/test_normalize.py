import unittest
from ketris_dashboard.models import ObservedValue,ViewSnapshotV1
from ketris_dashboard.normalize import CounterPoint,counter_delta,partition_shares,normalize_runtime

class NormalizeTests(unittest.TestCase):
    def test_missing_zero_and_nan_are_distinct(self):
        self.assertIsNone(ObservedValue(value=None,unit='us',source='runtime',quality='unavailable').value)
        self.assertEqual(ObservedValue(value=0,unit='us',source='runtime',quality='observed').value,0)
        with self.assertRaises(ValueError):ObservedValue(value=float('nan'),source='runtime',quality='observed')
    def test_counter_window_generation_reset_and_missing(self):
        p=CounterPoint(10,1,'g','migA');c=CounterPoint(20,2,'g','migA')
        self.assertEqual(counter_delta(p,c).delta,10)
        for bad in [CounterPoint(1,2,'g','migA'),CounterPoint(20,2,'new','migA'),CounterPoint(20,33,'g','migA'),CounterPoint(None,2,'g','migA'),CounterPoint(20,2,'g','migB')]:
            self.assertFalse(counter_delta(p,bad).valid)
    def test_domains_and_partial_denominator(self):
        streams={'A':counter_delta(CounterPoint(0,1,'g','migA'),CounterPoint(30,2,'g','migA')),'B':counter_delta(CounterPoint(0,1,'g','migA'),CounterPoint(10,2,'g','migA')),'C':counter_delta(CounterPoint(0,1,'g','migB'),CounterPoint(5,2,'g','migB'))}
        self.assertEqual(partition_shares(streams)['migA'].shares,{'A':.75,'B':.25})
        self.assertEqual(partition_shares(streams)['migB'].shares,{'C':1.0})
        streams['B']=counter_delta(None,CounterPoint(10,2,'g','migA'))
        self.assertEqual(partition_shares(streams)['migA'].shares,{'A':None,'B':None})
    def test_runtime_policy_and_observation_remain_separate(self):
        state={'schema_version':1,'node':'n','gpu_uuid':'GPU-895722c8-b706-27ae-018d-c43e405e6c35','mig_uuid':'MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf','generation':'g','inventory':{'profile':'1g.24gb','profile_sm':46},'observed_at':'2026-10-13T00:00:00Z','manager_state':'running','tenants':[{'allocation_id':'a','pod_uid':'uid','container':'c','space_percent':50,'time_weight':3,'reported_sm_count':22,'configured_share':.75,'observed_share':.749,'runtime_generation':'pid','phase':'armed'}]}
        s=normalize_runtime(state)
        w=s.workloads[0]
        self.assertEqual(w.requested['space_percent'].value,50)
        self.assertEqual(w.applied['reported_sm_count'].value,22)
        self.assertEqual(w.observed['charged_share'].value,.749)
        self.assertEqual(w.applied['time_share'].quality,'configured')
        self.assertIn('uid',w.id);self.assertIn('c',w.id)
        state['gpu_uuid']='default'
        with self.assertRaises(ValueError):normalize_runtime(state)

    def test_missing_replay_timestamp_is_not_fabricated(self):
        self.assertIsNone(ViewSnapshotV1(node='archive',mode='replay',captured_at=None).captured_at)
        with self.assertRaises(ValueError):ViewSnapshotV1(node='n',mode='live',captured_at=None)
