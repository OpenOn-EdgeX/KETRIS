import unittest,importlib.util,json
from pathlib import Path
class Watch(unittest.TestCase):
 def test_selects_actual_rejected_event_before_kueue_resets_entries(self):
  s=importlib.util.spec_from_file_location('e2e',Path(__file__).parents[1]/'tools/run_e2e.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  before={'object':{'status':{'admissionChecks':[{'name':'keti-scheduler','state':'Ready'},{'name':'ketris-plan-validation','state':'Pending'}]}}};rejected=json.loads(json.dumps(before));rejected['object']['status']['admissionChecks'][1]['state']='Rejected';reset=json.loads(json.dumps(before));reset['object']['status']['admissionChecks'][0]['state']='Pending'
  text='\n'.join(json.dumps(x,indent=2) for x in (before,rejected,reset))+'\n{"type":'
  self.assertEqual(m.select_guard_rejection(text),rejected['object']);self.assertIsNone(m.select_guard_rejection(json.dumps(reset)))
 def test_rollout_wait_outlives_kubectl_server_timeout(self):
  from unittest.mock import patch
  s=importlib.util.spec_from_file_location('e2e_timeout',Path(__file__).parents[1]/'tools/run_e2e.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  with patch.object(m.subprocess,'run') as run:
   run.return_value.returncode=0;run.return_value.stdout='ready'
   m.kub(['rollout','status','deployment/test','--timeout=180s'])
   self.assertGreater(run.call_args.kwargs['timeout'],180)
 def test_collects_live_snapshot_while_cohort_is_running(self):
  import tempfile
  from unittest.mock import patch,MagicMock
  s=importlib.util.spec_from_file_location('e2e_live',Path(__file__).parents[1]/'tools/run_e2e.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as d,patch('urllib.request.urlopen') as fetch:
   response=MagicMock();response.read.return_value=b'{"captured_at":"actual","workloads":[]}'
   fetch.return_value.__enter__.return_value=response;m.capture_dashboard(Path(d))
   self.assertEqual(json.loads((Path(d)/'dashboard-active.json').read_text())['captured_at'],'actual')
