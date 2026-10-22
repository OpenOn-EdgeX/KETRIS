import unittest
from ketris_mig.credit_experiment import sm_time_grants

class CreditExperimentTests(unittest.TestCase):
    def test_idle_a_lends_to_all_three_spatially_limited_backgrounds(self):
        # Three 14/46-SM clients can each run the full wall-clock tick.
        self.assertEqual(sm_time_grants({'A':46,'B1':14,'B2':14,'B3':14},
            {'B1':1,'B2':1,'B3':1},46,10000),{'A':0,'B1':10000,'B2':10000,'B3':10000})

    def test_active_ratio_uses_resource_time_instead_of_sum_of_worker_time(self):
        self.assertEqual(sm_time_grants({'A':46,'B1':14,'B2':14,'B3':14},
            {'A':9,'B1':1,'B2':1,'B3':1},46,10000),
            {'A':7500,'B1':2738,'B2':2738,'B3':2738})

    def test_spatially_capped_a_cannot_receive_more_than_one_wall_tick(self):
        # 50% requested space is actually 22 of 46 SM on this MIG.
        self.assertEqual(sm_time_grants({'A':22,'B':22},{'A':3,'B':1},46,10000),
                         {'A':10000,'B':10000})

    def test_overcommitted_backgrounds_share_one_mig_resource_time_budget(self):
        self.assertEqual(sm_time_grants({'A':46,'B1':22,'B2':22,'B3':22},
            {'B1':1,'B2':1,'B3':1},46,10000),
            {'A':0,'B1':6969,'B2':6969,'B3':6969})

    def test_no_demand_grants_nothing_and_invalid_identity_is_rejected(self):
        self.assertEqual(sm_time_grants({'A':46},{},46,10000),{'A':0})
        for sm,weights,total,tick in [({'A':47},{'A':1},46,10000),
            ({'A':46},{'missing':1},46,10000),({'A':46},{'A':0},46,10000),
            ({'A':True},{'A':1},46,10000),({'A':46},{'A':1},46,0)]:
            with self.assertRaises(ValueError):sm_time_grants(sm,weights,total,tick)

    def test_mass_conservation_across_all_candidate_caps_and_ratios(self):
        for a in [22,26,36,46]:
            for b in [8,14,18,22,46]:
                for weight in [1,2,3,5]:
                    sm={'A':a,'B1':b,'B2':b,'B3':b}
                    out=sm_time_grants(sm,{'A':3*weight,'B1':1,'B2':1,'B3':1},46,10000)
                    self.assertLessEqual(sum(sm[k]*out[k] for k in sm),460000)
                    self.assertTrue(all(0<=v<=10000 for v in out.values()))
