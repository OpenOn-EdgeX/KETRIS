from pathlib import Path
import tempfile
import unittest
from ketris_dashboard.store import ExperimentStore
from ketris_dashboard.archive import parse_experiment
from ketris_dashboard.models import ViewSnapshotV1
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.path=Path(self.t.name)/'db.sqlite';self.s=ExperimentStore(self.path);self.addCleanup(lambda:self.s.close())
    def snapshot(self,at='2026-10-13T00:00:00Z'):
        return ViewSnapshotV1(node='n',captured_at=at)
    def test_original_archive_idempotence_pagination_and_reopen(self):
        b=parse_experiment(Path('/root/workspace/prism-server-task/reports/ketris_mig_repro_20261012'))
        id=self.s.import_bundle(b);self.assertEqual(id,self.s.import_bundle(b))
        self.assertEqual(len(self.s.load_frames(id,0,500)),12)
        self.assertEqual(len(self.s.load_frames(id,2,2)),2)
        with self.assertRaises(ValueError):self.s.load_frames(id,0,501)
        self.s.close();self.s=ExperimentStore(self.path)
        self.assertEqual(len(self.s.load_frames(id,0,500)),12)
        self.assertIsNotNone(self.s.evidence(b.frames[0].evidence_ids[0]))
        with self.assertRaises(ValueError):self.s.evidence('../../etc/passwd')
    def test_sessions_sequence_timestamp_and_history_survive_restart(self):
        self.s.accept('s1',3,self.snapshot())
        with self.assertRaises(ValueError):self.s.accept('s1',2,self.snapshot())
        self.s.accept('s2',0,self.snapshot('2026-10-13T00:00:02Z'))
        with self.assertRaises(ValueError):self.s.accept('s1',10,self.snapshot('2026-10-13T00:00:03Z'))
        with self.assertRaises(ValueError):self.s.accept('s3',0,self.snapshot('2026-10-12T00:00:00Z'))
        self.s.close();self.s=ExperimentStore(self.path)
        with self.assertRaises(ValueError):self.s.accept('s1',99,self.snapshot('2026-10-13T00:00:04Z'))
        self.assertEqual(self.s.snapshot('n')['captured_at'],'2026-10-13T00:00:02Z')
        self.assertEqual(len(self.s.history('n',0,500)),1)
