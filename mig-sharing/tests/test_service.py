import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from ketris_mig.service import NodeRuntime
from ketris_mig.inventory import MigInventoryEntry
from test_feeder import M,G

class ServiceTests(unittest.TestCase):
    def fixture(self, root):
        other='MIG-67155f7e-c1c5-5cde-a89f-7ee600150005'
        entries=[MigInventoryEntry(G,0,uuid,i,0,'1g.24gb',24000,46,'g'+str(i)) for i,uuid in enumerate([M,other])]
        api=Mock();api.pods.return_value=[]
        def manager(entry, root):
            m=Mock();m.ensure.return_value=m;m.pipe=Path(root)/entry.mig_uuid;return m
        def spawn(*args):
            p=Mock();p.poll.return_value=None;return p
        rt=NodeRuntime('node',root,api,inventory_reader=lambda:entries,manager_factory=manager,spawn=Mock(side_effect=spawn),allowlist=[M,other])
        rt.socket_dir=root;rt.checkpoint=lambda:{'Data':{'PodDeviceEntries':[]}}
        return rt,entries

    def test_kubelet_restart_reregisters_every_mig(self):
        with tempfile.TemporaryDirectory() as root:
            rt,entries=self.fixture(root);sock=Path(root)/'kubelet.sock';sock.touch();rt.refresh()
            old=list(rt.children.values());replacement=Path(root)/'replacement';replacement.touch();replacement.replace(sock)
            rt.refresh()
            self.assertEqual(rt.spawn.call_count,4)
            for child in old:child.terminate.assert_called_once()

    def test_removed_partitions_retire_owned_managers_and_exports(self):
        with tempfile.TemporaryDirectory() as root:
            rt,entries=self.fixture(root);rt.refresh();rt.sample_and_export()
            managers=list(rt.managers.values());children=list(rt.children.values());entries.clear();rt.refresh();rt.sample_and_export()
            for manager in managers:manager.stop.assert_called_once_with(0)
            for child in children:child.terminate.assert_called_once()
            self.assertEqual(rt.controllers,{})
            self.assertEqual(rt.managers,{})
            self.assertEqual(list((Path(root)/'partitions').glob('*.json')),[])

    def test_removed_partition_preserves_verified_running_client_until_release(self):
        from ketris_mig.state import atomic_json,read_json
        from ketris_mig.contracts import AllocationBindingV1
        with tempfile.TemporaryDirectory() as root:
            rt,entries=self.fixture(root);rt.refresh();entry=entries[0];key=entry.mig_uuid+':'+entry.generation;manager=rt.managers[key]
            b=AllocationBindingV1('a','node',G,entry.mig_uuid,entry.generation,entry.resource_name,['x'+str(i) for i in range(50)],50,str(Path(root)/'control/a'))
            atomic_json(Path(root)/'allocations/a.json',b.to_dict())
            atomic_json(Path(root)/'bindings/a.json',{**b.to_dict(),'pod_uid':'uid','container':'work','container_id':'containerd://abc','time_weight':1,'actual_cuda_uuid':entry.mig_uuid,'phase':'bound'})
            rt.api.pods.return_value=[{'metadata':{'uid':'uid'},'status':{'containerStatuses':[{'name':'work','containerID':'containerd://abc','state':{'running':{}}}]}}]
            entries.clear();rt.refresh();rt.sample_and_export()
            manager.stop.assert_not_called()
            export=read_json(next((Path(root)/'partitions').glob('*.json')))
            self.assertEqual(export['manager_state'],'retired')
            binding=read_json(Path(root)/'bindings/a.json');binding.pop('actual_cuda_uuid',None)
            self.assertEqual(AllocationBindingV1.from_dict(binding).phase,'failed')
            rt.refresh();manager.stop.assert_not_called()
            rt.api.pods.return_value=[];rt.refresh()
            manager.stop.assert_called_once_with(0)

    def test_replaced_generation_does_not_disable_current_plugin_config(self):
        from dataclasses import replace
        from ketris_mig.state import read_json
        with tempfile.TemporaryDirectory() as root:
            rt,entries=self.fixture(root);rt.refresh()
            entries[0]=replace(entries[0],generation='replacement');rt.refresh()
            key=entries[0].mig_uuid+':replacement'
            self.assertTrue(read_json(rt.configs[key][0])['active'])

    def test_source_failure_disarms_existing_domain(self):
        with tempfile.TemporaryDirectory() as root:
            api=Mock();api.pods.side_effect=RuntimeError('api down')
            runtime=NodeRuntime('node',root,api=api,inventory_reader=lambda:[])
            controller=Mock();runtime.controllers={'old':controller}
            runtime.refresh()
            controller.reconcile.assert_called_once_with([])
            self.assertEqual(runtime.status,'unavailable')

    def test_duplicate_start_prevented_and_catalog_published(self):
        with tempfile.TemporaryDirectory() as root:
            api=Mock();api.pods.return_value=[]
            entry=MigInventoryEntry(G,0,M,1,0,'1g.24gb',24000,46,'g')
            factory=Mock();manager=factory.return_value;manager.ensure.return_value=manager;manager.pipe=Path(root)/'pipe';manager.pid=123
            process=Mock();process.poll.return_value=None
            spawn=Mock(return_value=process)
            rt=NodeRuntime('node',root,api=api,inventory_reader=lambda:[entry],manager_factory=factory,spawn=spawn,allowlist=[M])
            rt.checkpoint=lambda:{'Data':{'PodDeviceEntries':[]}}
            rt.refresh();rt.refresh()
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(api.publish.call_count,2)
            self.assertEqual(rt.status,'ok')

    def test_unlisted_mig_is_not_managed(self):
        with tempfile.TemporaryDirectory() as root:
            api=Mock();api.pods.return_value=[]
            entry=MigInventoryEntry(G,0,M,1,0,'1g.24gb',24000,46,'g')
            factory=Mock()
            rt=NodeRuntime('node',root,api,inventory_reader=lambda:[entry],manager_factory=factory,allowlist=[])
            rt.checkpoint=lambda:{'Data':{'PodDeviceEntries':[]}}
            rt.refresh();factory.assert_not_called()
            self.assertEqual(api.publish.call_args[0][1]['instances'],[])

