import unittest
from policy import credit_grants, whole_makespan

class PolicyTests(unittest.TestCase):
    def test_seven_clients_keep_individual_walltime_credit(self):
        phases={f'W{i}':'decode' for i in range(7)}
        self.assertEqual(credit_grants(phases,10000,'phase75'),{w:10000 for w in phases})
    def test_only_overlapping_prefill_reduces_supply(self):
        self.assertEqual(credit_grants({'W0':'prefill','W1':'decode','W2':'done'},10000,'phase75'),{'W0':7500,'W1':10000,'W2':0})
        self.assertEqual(credit_grants({'W0':'prefill'},10000,'phase75'),{'W0':10000})
    def test_invalid_phase_fails(self):
        with self.assertRaises(ValueError):credit_grants({'W0':'unknown'},10000,'phase90')
    def test_last_worker_defines_completion(self):
        done={f'W{i}':100+i for i in range(7)}
        self.assertEqual(whole_makespan(10,done),96)
    def test_missing_worker_is_not_success(self):
        with self.assertRaises(ValueError):whole_makespan(10,{'W0':100})
if __name__=='__main__':unittest.main()
