import json
from pathlib import Path
import tempfile
import unittest
from ketris_dashboard.archive import parse_experiment

REPORTS=Path('/root/workspace/prism-server-task/reports')
class ArchiveTests(unittest.TestCase):
    def test_gpu_counter_evidence_filters_nested_private_fields_and_invalid_numbers(self):
        from ketris_dashboard.archive import _gpu_frame
        row={'config':'solo100','repeat':1,'workers':{'A':{'runtime_delta':{'charged_us':5,'kernels':1,'prompt':'TEST_PRIVATE_SENTINEL','secret':{'token':'private'}}}}}
        frame,evidence=_gpu_frame(row,0,False,{})
        self.assertEqual(evidence['A']['runtime_delta'],{'charged_us':5,'kernels':1})
        self.assertNotIn('TEST_PRIVATE_SENTINEL',json.dumps(evidence))
        for invalid in [True,-1,'5',float('nan'),float('inf')]:
            row['workers']['A']['runtime_delta']['charged_us']=invalid
            frame,evidence=_gpu_frame(row,0,False,{})
            self.assertIsNone(evidence['A']['runtime_delta']['charged_us'])
            self.assertIsNone(frame.snapshot.workloads[0].observed['charged_share'].value)
    def test_original_mig_values_and_historical_identity(self):
        path=REPORTS/'ketris_mig_repro_20261012';b=parse_experiment(path)
        self.assertEqual(b.experiment_id,parse_experiment(path).experiment_id)
        self.assertEqual(b.summary['combined50_50_time3_1']['charged_share_a_mean'],.7493978394906334)
        self.assertAlmostEqual(b.summary['space50_50']['total_ips_mean'],128.48746805963344)
        f=next(f for f in b.frames if f.interval['condition']=='combined50_50_time3_1')
        self.assertEqual([w.applied['reported_sm_count'].value for w in f.snapshot.workloads],[22,22])
        self.assertTrue(f.snapshot.partitions[0].historical)
        self.assertIsNone(f.snapshot.workloads[0].applied['actual_cuda_uuid'].value)
        self.assertIn('MOCK',f.snapshot.workloads[0].origin)
        self.assertEqual(f.axis_kind,'measurement_window');self.assertIsNone(f.snapshot.captured_at)
        self.assertEqual(len(b.frames),12)
        self.assertFalse(any('gemm.so' in r['path'] for r in b.source_manifest))

    def test_rngd_precision_completion_latency_and_projection(self):
        b=parse_experiment(REPORTS/'ketris_rngd_repro_20261012')
        self.assertEqual(b.summary['spacetime_70_30']['tenants']['A']['contract_time_share'],.5738517933467682)
        f=next(f for f in b.frames if f.interval['condition']=='spacetime_70_30')
        self.assertEqual([p.pe_indices for p in f.snapshot.partitions],[[0,1,2,3],[4,5,6,7]])
        self.assertIn('p95_completion_ms',f.snapshot.workloads[0].observed)
        encoded=json.dumps(b.to_dict())
        for key in ['"requests":','"prompt":','"generated_text":','"env":','"ttft_ms":']:
            self.assertNotIn(key,encoded)
        self.assertIn('not_reproduced',b.metadata['precision_verdict'])

    def test_host_gpu_no_invented_pod_and_path_escape(self):
        b=parse_experiment(REPORTS/'ketris_gpu_repro_20261012')
        for frame in b.frames:
            self.assertTrue(all(w.pod_uid is None and w.origin=='host process' for w in frame.snapshot.workloads))
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):parse_experiment(Path(t))
        with self.assertRaises(ValueError):parse_experiment(REPORTS/'ketris_mig_repro_20261012/../ketris_rngd_repro_20261012/furiosa-cache')

    def test_used_source_symlink_escape_and_text_projection(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'ketris_mig_fixture';root.mkdir()
            summary=json.loads((REPORTS/'ketris_mig_repro_20261012/summary.json').read_text())
            summary['summary']['solo100']['worker_ips']['A']='<script>visible text</script>'
            summary['summary']['solo100']['env']={'SECRET':'not published'}
            (root/'summary.json').write_text(json.dumps(summary))
            b=parse_experiment(root,allowed_roots=[root])
            self.assertEqual(b.summary['solo100']['worker_ips']['A'],'<script>visible text</script>')
            self.assertNotIn('SECRET',json.dumps(b.to_dict()))
            (root/'summary.json').unlink();(root/'summary.json').symlink_to(REPORTS/'ketris_mig_repro_20261012/summary.json')
            with self.assertRaises(ValueError):parse_experiment(root,allowed_roots=[root])

    def test_new_runtime_records_preserve_actual_cuda_identity_and_requests(self):
        root=Path(__file__).resolve().parents[3]/'reports/ketris_mig_runtime_20261013_full'
        b=parse_experiment(root,allowed_roots=[root])
        frame=next(f for f in b.frames if f.interval['condition']=='combined50_50_time3_1')
        w=frame.snapshot.workloads[0]
        self.assertEqual(w.requested['space_percent'].value,50)
        self.assertEqual(w.applied['actual_cuda_uuid'].quality,'observed')
        self.assertTrue(w.applied['actual_cuda_uuid'].value.startswith('MIG-'))
        self.assertEqual(w.applied['time_share'].value,.75)
        self.assertIsNotNone(w.pod_uid)
