import importlib.util
from pathlib import Path
import tempfile
import unittest

class HarnessTests(unittest.TestCase):
    def test_report_argument_resolves_to_absolute_host_mount(self):
        path=Path(__file__).parents[1]/'tools/verify_live.py'
        spec=importlib.util.spec_from_file_location('verify_live',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory(dir='.') as d:
            out=Path(d).relative_to(Path.cwd())/'report';e=m.Experiment(out)
            self.assertTrue(e.out.is_absolute())
            self.assertEqual(e.out,out.resolve())
