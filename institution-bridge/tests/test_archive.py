import unittest,tempfile
from pathlib import Path
class Archive(unittest.TestCase):
 def test_replay_manifest_links_exact_measurements(self):
  from ketris_integration.archive import make_replay_bundle
  with tempfile.TemporaryDirectory() as d:self.assertRaises(ValueError,make_replay_bundle,Path(d))
 def test_mock_profile_not_labeled_measured_prediction(self):
  from ketris_integration.archive import profile_origin
  self.assertEqual(profile_origin({'meta':{'origin':'SYNTHETIC'},'gpu_memory_source':'SYNTHETIC'}),'SYNTHETIC 기관 입력 · 실제 RPC/GPU 실행')
