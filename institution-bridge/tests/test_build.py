import unittest,tempfile,importlib.util
from pathlib import Path
class Build(unittest.TestCase):
 def test_build_context_excludes_models_reports_credentials(self):
  spec=importlib.util.spec_from_file_location('build',Path(__file__).parents[1]/'tools/build.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as d:
   result=m.stage_sources(Path(d));paths={str(p.relative_to(d)) for p in Path(d).rglob('*') if p.is_file()}
   self.assertIn('workload/worker.py',paths);self.assertFalse(any('reports/' in x or 'token' in x or 'model.safetensors' in x for x in paths));self.assertTrue(result['source_sha256'])
 def test_generated_proto_and_runtime_versions_match(self):
  from ketris_integration.rpc import institution_bridge_pb2 as pb
  import google.protobuf
  self.assertEqual(pb.DESCRIPTOR.package,'ketris.integration.v1');self.assertEqual(google.protobuf.__version__,'7.35.1')
 def test_images_override_inherited_plugin_entrypoint(self):
  # Entrypoint is exercised by actual image smoke commands in Task7/8, not a source grep.
  from ketris_integration.jobs import build_job
  from ketris_integration.contracts import ExecutionRecord,parse_profile,parse_plan
  from ketris_integration.rpc import institution_bridge_pb2 as pb
  from fixtures import profile_dict,plan_dict,NOW
  r=ExecutionRecord('work-a',parse_profile(pb.PushProfileRequest(**profile_dict()),NOW),parse_plan(pb.SubmitPartitionPlanRequest(**plan_dict()),NOW))
  self.assertEqual(build_job(r,'worker')['spec']['template']['spec']['containers'][0]['command'],['python3','/app/worker.py'])
