import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime,timezone
from ketris_mig.bindings import reconcile_bindings
from ketris_mig.contracts import AllocationBindingV1,resource_name
from ketris_mig.inventory import MigInventoryEntry
from test_feeder import M,G

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='kb-',dir='/tmp');self.root=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.entry=MigInventoryEntry(G,0,M,1,0,'1g.24gb',24000,46,'gen')
        d=self.root/'control'/'a-123';d.mkdir(parents=True)
        self.b=AllocationBindingV1('a-123','node',G,M,'gen',resource_name(M),['x'],1,str(d))
        self.cp={'Data':{'PodDeviceEntries':[{'PodUID':'uid','ContainerName':'worker','ResourceName':resource_name(M),'DeviceIDs':{'-1':['x']}}]}}
        self.pod={'metadata':{'uid':'uid','name':'same','annotations':{'ketris.re.kr/time-weight':'3'}},'spec':{'containers':[{'name':'worker','resources':{'limits':{resource_name(M):1},'requests':{resource_name(M):1}},'env':[{'name':'KETRIS_TIME_WEIGHT','value':'3'}]}]},'status':{'containerStatuses':[{'name':'worker','containerID':'containerd://id','state':{'running':{'startedAt':'2026-01-01T00:00:00Z'}}}]}}
        (d/'bless-9.sock').touch()
        self.info={'actual_cuda_uuid':M,'device_count':1,'cuda_error':0,'sm_error':0,'reported_sm_count':1,'pid':9,'runtime_generation':'9-1','start_gated':True,'observed_at_us':datetime.now(timezone.utc).timestamp()*1e6}
        (d/'device.json').write_text(json.dumps(self.info))

    def run_binding(self,prior=None):
        return reconcile_bindings([self.b],self.cp,[self.pod],[self.entry],self.root,prior or {})

    def test_verified_bound_and_unmatched_uid(self):
        self.assertEqual(self.run_binding()[0].phase,'bound')
        self.pod['metadata']['uid']='different'
        self.assertEqual(self.run_binding()[0].phase,'allocated')

    def test_actual_uuid_and_counter_generation(self):
        self.info['actual_cuda_uuid']=G
        (Path(self.b.control_dir)/'device.json').write_text(json.dumps(self.info))
        self.assertEqual(self.run_binding()[0].phase,'failed')

    def test_uninitialized_container_is_tracked_then_retired_after_release(self):
        (Path(self.b.control_dir)/'device.json').unlink()
        b=self.run_binding()[0]
        self.assertEqual(b.phase,'allocated');self.assertEqual(b.pod_uid,'uid')
        self.cp['Data']['PodDeviceEntries']=[];self.pod['status']['containerStatuses']=[]
        self.assertEqual(self.run_binding({'a-123':b.to_dict()})[0].phase,'completed')

    def test_uid_reuse_termination_and_path_validation(self):
        self.assertEqual(self.run_binding({'a-123':{'pod_uid':'old'}})[0].phase,'failed')
        self.pod['status']['containerStatuses'][0]['state']={'terminated':{'exitCode':0}}
        self.cp['Data']['PodDeviceEntries']=[]
        self.assertEqual(self.run_binding({'a-123':{'pod_uid':'uid'}})[0].phase,'completed')
        self.b.control_dir='/tmp'
        with self.assertRaises(ValueError): self.run_binding()

