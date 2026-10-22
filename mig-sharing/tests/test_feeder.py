import unittest
from ketris_mig.feeder import PartitionController, split_budget
from ketris_mig.inventory import MigInventoryEntry

M='MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf'
G='GPU-895722c8-b706-27ae-018d-c43e405e6c35'

class FeederTests(unittest.TestCase):
    def test_exact_split_and_invalid_weights(self):
        self.assertEqual(split_budget({'A':3,'B':1}),{'A':7500,'B':2500})
        self.assertEqual(sum(split_budget({'A':1,'B':1,'C':1}).values()),10000)
        for w in [0,-1,True,1.5,float('nan')]:
            with self.assertRaises(ValueError): split_budget({'A':w})

    def make(self,m=M):
        calls=[]
        entry=MigInventoryEntry(G,0,m,1,0,'1g.24gb',24000,46,'generation')
        return PartitionController('node',entry,lambda s,c:calls.append((s,c))),calls

    def tenant(self,**kw):
        return dict(allocation_id='A',socket='/tmp/a.sock',runtime_generation='pid1',
                    phase='bound',time_weight=3,space_percent=50,pod_uid='uid',container='w',**kw)

    def test_arm_zero_credit_and_isolate_domains(self):
        a,ca=self.make();b,cb=self.make(M.replace('5ffbbadf','6ffbbadf'))
        a.reconcile([self.tenant()]);b.reconcile([self.tenant()])
        self.assertEqual(ca[0][1],'time_credit 0')
        a.tick();self.assertEqual(ca[-1][1],'time_add 10000')
        self.assertEqual(len(cb),1)
        a.reconcile([]);self.assertEqual(ca[-1][1],'time_credit 0')
        n=len(ca);a.tick();self.assertEqual(len(ca),n)

    def test_unbound_no_credit_and_pid_generation_resets(self):
        a,c=self.make();t=self.tenant();t['phase']='allocated'
        a.reconcile([t]);a.tick();self.assertEqual(c,[])
        t['phase']='bound';a.reconcile([t]);a.tick()
        t['runtime_generation']='pid2';a.reconcile([t])
        self.assertEqual(c[-1][1],'time_credit 0')

    def test_counter_reset_partial_and_interval(self):
        a,c=self.make();a.reconcile([self.tenant()])
        a.observe('A',100,2,1);self.assertIsNone(a.tenants['A']['observed_share'])
        a.observe('A',110,3,2);self.assertEqual(a.tenants['A']['observed_share'],1.0)
        a.observe('A',1,1,3);self.assertIsNone(a.tenants['A']['observed_share'])
        a.observe('A',12,2,40);self.assertIsNone(a.tenants['A']['observed_share'])
        state=a.export_state().to_dict()
        self.assertEqual(state['mig_uuid'],M)
        self.assertEqual(state['tenants'][0]['configured_share'],1.0)

    def test_verified_cuda_sm_survives_arm_and_reconcile(self):
        a,c=self.make();t=self.tenant(reported_sm_count=22)
        a.reconcile([t]);self.assertEqual(a.export_state().tenants[0]['reported_sm_count'],22)
        a.reconcile([t]);self.assertEqual(a.export_state().tenants[0]['reported_sm_count'],22)

