import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ketris_mig.inventory import MigInventoryEntry
from ketris_mig.state import atomic_json
from test_feeder import G,M

class HarnessPathTests(unittest.TestCase):
    def test_probe_reads_current_generation_config_and_rejects_inactive(self):
        source=Path(__file__).resolve().parents[1]/'tools/verify_live.py'
        spec=importlib.util.spec_from_file_location('ketris_live_harness',source)
        harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)
        entry=MigInventoryEntry(G,0,M,3,0,'1g.24gb',24192,46,'generation')
        with tempfile.TemporaryDirectory() as root,patch.object(harness,'STATE',Path(root)):
            experiment=harness.Experiment(Path(root)/'report')
            path=Path(root)/'plugins'/(entry.resource_name.split('/')[-1]+'-'+entry.generation+'.json')
            config={'mig_uuid':M,'generation':entry.generation,'active':True,'pipe_dir':'/owned/current'}
            atomic_json(path,config)
            self.assertEqual(experiment.manager_pipe(entry),'/owned/current')
            config['active']=False;atomic_json(path,config)
            with self.assertRaises(ValueError):experiment.manager_pipe(entry)
