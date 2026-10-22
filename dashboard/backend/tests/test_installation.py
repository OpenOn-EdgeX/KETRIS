import re
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]

class InstallationTests(unittest.TestCase):
    def test_default_build_images_match_manifests_and_dashboard_base(self):
        def tag(path):return re.search(r'TAG=\$\{1:-([^}]+)\}',(ROOT/path).read_text())[1]
        mig='ketris-device-plugin:'+tag('mig-sharing/tools/build.sh')
        dash='ketris-dashboard:'+tag('dashboard/tools/build.sh')
        for path in ['dashboard/deploy/deployment.yaml','dashboard/deploy/observer.yaml']:
            self.assertEqual(re.search(r'image: (\S+)',(ROOT/path).read_text())[1],dash)
        self.assertIn('ARG BASE_IMAGE='+mig,(ROOT/'dashboard/Dockerfile').read_text())
        self.assertIn('image: '+mig,(ROOT/'mig-sharing/deploy/daemonset.yaml').read_text())
