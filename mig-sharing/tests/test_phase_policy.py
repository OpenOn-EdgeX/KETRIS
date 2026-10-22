import unittest
from ketris_mig.phase_policy import PhasePolicy

class PhasePolicyTest(unittest.TestCase):
    def test_phase_changed_during_drain_is_observed_before_next_launch(self):
        p=PhasePolicy(94,48,8,3,100)
        state=['idle',100]
        def drain():state[0]='prefill'
        decision=p.after_drain(lambda:tuple(state),drain,lambda:101)
        self.assertTrue(decision['pause_background'])
        self.assertEqual(decision['ranges']['B1'],(48,16))
    def test_backgrounds_can_share_available_remainder(self):
        p=PhasePolicy(94,48,8,3,100,share_background=True)
        ranges=p.decide('decode',100,101)['ranges']
        self.assertEqual(ranges,{'A':(0,8),'B1':(8,86),'B2':(8,86),'B3':(8,86)})
        self.assertEqual(p.decide('idle',100,101)['ranges']['B3'],(0,94))
    def test_idle_lends_all_capacity_and_prefill_reclaims_it(self):
        p=PhasePolicy(total_tpcs=94, prefill_tpcs=48, decode_tpcs=8, backgrounds=3, stale_ns=100)
        self.assertEqual(p.decide('idle', 100, 101)['ranges'], {'A':(0,48),'B1':(0,32),'B2':(32,31),'B3':(63,31)})
        r=p.decide('prefill',100,101)
        self.assertEqual(r['ranges'],{'A':(0,48),'B1':(48,16),'B2':(64,15),'B3':(79,15)})
        self.assertEqual(r['pause_background'],True)
        self.assertEqual(p.decide('decode',100,101)['ranges']['B1'],(8,29))
    def test_stale_or_unknown_phase_cannot_pause_forever(self):
        p=PhasePolicy(94,48,8,3,100)
        for phase,stamp,now in [('prefill',0,101),('wrong',100,101),('prefill',102,101)]:
            r=p.decide(phase,stamp,now)
            self.assertTrue(r['stale']);self.assertFalse(r['pause_background'])
            self.assertEqual(r['ranges']['B1'],(0,32))
    def test_partitions_fit_and_cover_available_capacity(self):
        for n in [1,3]:
            p=PhasePolicy(94,48,8,n,100)
            for phase in ['prefill','decode']:
                ranges=p.decide(phase,100,101)['ranges']
                used=[]
                for start,count in ranges.values():used.extend(range(start,start+count))
                self.assertEqual(sorted(used),list(range(94)))
    def test_bad_configuration_rejected(self):
        for args in [(94,94,8,3,100),(94,48,0,3,100),(94,48,8,0,100)]:
            with self.assertRaises(ValueError):PhasePolicy(*args)
