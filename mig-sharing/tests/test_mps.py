from pathlib import Path
import tempfile
import unittest
import subprocess
from ketris_mig.mps import MpsManager
from test_feeder import M,G
from ketris_mig.inventory import MigInventoryEntry

class MpsTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.e=MigInventoryEntry(G,0,M,1,0,'1g.24gb',24000,46,'gen')
        self.calls=[]
        def runner(args,**kw):
            self.calls.append(args)
            if args[0]=='systemd-run':
                (self.m.pipe/'nvidia-cuda-mps-control.pid').write_text('123')
            return ''
        self.m=MpsManager(self.e,Path(self.t.name),runner,lambda pid:pid==123)

    def test_owned_manager_reused_without_stopping_external(self):
        self.m.ensure();self.m.ensure()
        self.assertEqual(sum(x[0]=='systemd-run' for x in self.calls),1)
        self.assertEqual(self.m.pid,123)
        self.m.stop(tenants=1)
        self.assertFalse(any('stop' in x for x in self.calls))
        self.m.stop(tenants=0)
        self.assertEqual(self.calls[-1],['systemctl','stop',self.m.unit])

    def test_external_pipe_is_never_adopted_or_stopped(self):
        self.m.pipe.mkdir(parents=True)
        (self.m.pipe/'nvidia-cuda-mps-control.pid').write_text('123')
        with self.assertRaises(RuntimeError): self.m.ensure()
        self.m.stop(tenants=0)
        self.assertEqual(self.calls,[])

    def test_first_start_does_not_reset_nonexistent_unit(self):
        runner=self.m.runner
        def checked(args,**kw):
            if args[:2]==['systemctl','reset-failed']:
                raise subprocess.CalledProcessError(1,args,stderr='Unit not loaded')
            if args[:2]==['systemctl','show']: return 'not-found\n'
            return runner(args,**kw)
        self.m.runner=checked
        self.m.ensure();self.assertEqual(self.m.pid,123)

