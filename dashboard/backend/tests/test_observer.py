import tempfile,time
import json,os
from pathlib import Path
import unittest
from ketris_dashboard.observer.sources import SourceCache,parse_npu_processes,ReadOnlySources
from ketris_dashboard.observer.service import NodeObserver
from ketris_dashboard.observer.bindings import checkpoint_workloads

class ObserverTests(unittest.TestCase):
    def test_actual_rngd_checkpoint_assigns_pe_block_on_npu_board(self):
        cp={'Data':{'PodDeviceEntries':[{'PodUID':'uid','ContainerName':'work','ResourceName':'keti.re.kr/npu-pe','DeviceIDs':{'-1':['keti-rngd0-pe0','keti-rngd0-pe1']}}]}}
        pod={'metadata':{'uid':'uid','name':'rngd-work'},'spec':{'containers':[{'name':'work','resources':{'limits':{'keti.re.kr/npu-pe':2}}}]},'status':{'phase':'Running'}}
        w=checkpoint_workloads('devmaster',cp,[pod])[0]
        self.assertEqual(w.partition_id,'devmaster:npu0')
        self.assertEqual(w.requested['pe_count'].value,2)
        self.assertEqual(w.applied['pe_indices'].value,'0,1')
        self.assertNotIn('lsu',w.requested)

    def test_unknown_npu_device_id_remains_npu_request_with_unavailable_assignment(self):
        cp={'Data':{'PodDeviceEntries':[{'PodUID':'uid','ContainerName':'work','ResourceName':'keti.re.kr/npu-pe','DeviceIDs':{'-1':['unknown-core']}}]}}
        pod={'metadata':{'uid':'uid','name':'rngd-work'},'spec':{'containers':[{'name':'work','resources':{'limits':{'keti.re.kr/npu-pe':1}}}]},'status':{'phase':'Running'}}
        w=checkpoint_workloads('devmaster',cp,[pod])[0]
        self.assertEqual(w.partition_id,'devmaster:npu0')
        self.assertEqual(w.requested['pe_count'].value,1)
        self.assertIsNone(w.applied['pe_indices'].value)
        self.assertNotIn('lsu',w.requested)

    def test_failed_checkpoint_and_kube_do_not_claim_fresh_assignment_or_running(self):
        cp={'Data':{'PodDeviceEntries':[{'PodUID':'uid','ContainerName':'work','ResourceName':'keti.re.kr/npu-pe','DeviceIDs':{'-1':['keti-npu0-pe4']}}]}}
        pod={'metadata':{'uid':'uid','name':'work'},'spec':{'containers':[{'name':'work','resources':{'limits':{'keti.re.kr/npu-pe':1}}}]},'status':{'phase':'Running'}}
        checkpoint=self.cache('checkpoint',cp);kube=self.cache('kubernetes',{'pods':[pod]})
        o=NodeObserver('n',{'checkpoint':checkpoint,'kubernetes':kube})
        fresh=o.collect_node().workloads[0];self.assertIsNotNone(fresh.applied['pe_indices'].observed_at)
        for c in [checkpoint,kube]:c.provider=lambda:(_ for _ in ()).throw(TimeoutError());c.poll()
        w=o.collect_node().workloads[0]
        self.assertEqual(w.applied['pe_indices'].quality,'stale');self.assertIsNotNone(w.applied['pe_indices'].observed_at)
        self.assertEqual(w.state,'stale')
        self.assertEqual(w.requested['pe_count'].quality,'stale')

    def test_stale_runtime_membership_suppresses_fresh_counter_share(self):
        with tempfile.TemporaryDirectory() as root:
            d=Path(root);(d/'feed.log').write_text('total=0 kernels=0\n')
            state={'schema_version':1,'node':'n','gpu_uuid':'GPU-895722c8-b706-27ae-018d-c43e405e6c35','mig_uuid':'MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf','generation':'g','budget_domain':'mig:g','inventory':{},'observed_at':'2026-10-13T00:00:00Z','manager_state':'running','tenants':[dict(allocation_id='a',pod_uid='a',container='c',phase='armed',space_percent=50,time_weight=1,runtime_generation='g',control_dir=root)]}
            rt=self.cache('runtime',[state]);o=NodeObserver('n',self.runtime_sources(rt,state));o.collect_node()
            rt.provider=lambda:(_ for _ in ()).throw(TimeoutError());rt.poll();(d/'feed.log').write_text('total=10 kernels=1\n')
            w=o.collect_node().workloads[0]
            self.assertIsNone(w.observed['charged_share'].value)
            self.assertIn('membership',w.observed['charged_share'].reason)

    def cache(self,name,value,interval=2):
        c=SourceCache(name,interval,lambda:value);c.poll();return c
    def runtime_sources(self,rt,state):
        pods=[{'metadata':{'uid':t['pod_uid'],'name':t['pod_uid']},'spec':{'containers':[{'name':t['container']}]}} for t in state['tenants']]
        return {'runtime':rt,'kubernetes':self.cache('kubernetes',{'pods':pods}),'gpu':self.cache('gpu',{'migs':[dict(mig_uuid=state['mig_uuid'],gpu_uuid=state['gpu_uuid'],generation=state['generation'])]})}
    def test_actual_idle_state_no_fake_workloads_or_default_uuid(self):
        gpu={'devices':[{'uuid':'GPU-895722c8-b706-27ae-018d-c43e405e6c35','name':'RTX PRO6000','memory_mib':97887,'memory_used_mib':60,'util_percent':0,'mig_mode':'Disabled'}],'migs':[]}
        sources={'gpu':self.cache('gpu',gpu,5),'npu':self.cache('npu',{'pe_count':8,'processes':[]},5),'runtime':self.cache('runtime',[]),'kubernetes':self.cache('kubernetes',{'pods':[],'checkpoint':{'Data':{'PodDeviceEntries':None}}},30),'controller':self.cache('controller',{'status':{'tenant_count':0},'capacity':{'uuid':'default'}})}
        s=NodeObserver('node',sources).collect_node()
        self.assertEqual(len(s.devices),2);self.assertEqual(s.workloads,[])
        self.assertEqual(s.devices[0].metrics['util_percent'].value,0)
        self.assertEqual(s.partitions[-1].pe_indices,list(range(8)))
        self.assertFalse(any('default' in d.id for d in s.devices))
    def test_partial_failure_and_stale_last_value(self):
        c=self.cache('npu',{'pe_count':8,'processes':[]},5)
        c.provider=lambda:(_ for _ in ()).throw(TimeoutError())
        c.poll();s=NodeObserver('n',{'npu':c}).collect_node()
        self.assertEqual(s.source_status['npu'].status,'stale')
        self.assertEqual(s.devices[0].metrics['pe_count'].quality,'stale')
        self.assertEqual(s.mode,'live')
    def test_npu_single_and_range_external_ownership(self):
        p=parse_npu_processes('| 99 | npu0:[3] | python |\n| 101 | npu0:[4, 7] | worker |')
        self.assertEqual([r['pe_indices'] for r in p],[[3],[4,5,6,7]])
    def test_pod_uid_and_container_checkpoint_keys(self):
        cp={'Data':{'PodDeviceEntries':[{'PodUID':'uid-old','ContainerName':'a','ResourceName':'keti.re.kr/npu-pe','DeviceIDs':{'-1':['keti-npu0-pe0']}},{'PodUID':'uid','ContainerName':'b','ResourceName':'keti.re.kr/npu-pe','DeviceIDs':{'-1':['keti-npu0-pe4']}}]}}
        pod={'metadata':{'name':'same','uid':'uid','namespace':'test'},'spec':{'containers':[{'name':'a'},{'name':'b','resources':{'limits':{'keti.re.kr/npu-pe':1}}}]},'status':{'phase':'Running'}}
        w=checkpoint_workloads('n',cp,[pod]);self.assertEqual(len(w),1);self.assertEqual(w[0].container,'b');self.assertEqual(w[0].pod_uid,'uid')
    def test_driverless_is_unavailable_not_replay(self):
        c=SourceCache('gpu',5,lambda:(_ for _ in ()).throw(FileNotFoundError()));c.poll()
        s=NodeObserver('n',{'gpu':c}).collect_node();self.assertEqual(s.mode,'live');self.assertEqual(s.devices,[])
    def test_sources_use_only_fixed_get_and_cli_and_fresh_export(self):
        calls=[]
        def get(url,**kw):
            calls.append(url)
            self.assertIn(url,['http://127.0.0.1:8090/status','http://127.0.0.1:8090/capacity'])
            return {}
        with tempfile.TemporaryDirectory() as root:
            src=ReadOnlySources('n',root,root,getter=get)
            src.controller();self.assertEqual(len(calls),2)
            self.assertEqual(src.runtime(),[])

    def test_actual_device_nodes_determine_idle_rngd_pe_count(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(8):(Path(root)/f'npu0pe{i}').touch()
            src=ReadOnlySources('n',dev_root=root,runner=lambda args,timeout:'| rngd | npu0 | 1.11.0 |' if args[-1]=='info' else '')
            self.assertEqual(src.npu()['pe_count'],8)

    def test_gate_stats_get_is_allowlisted_and_projected(self):
        urls=[]
        def getter(url,**kwargs):
            urls.append(url);return {'tenants':{'A':{'charged_us':10,'done':2,'prompt':'private','generated_text':'private'}}}
        src=ReadOnlySources('n',getter=getter,gate_endpoints=[{'url':'http://127.0.0.1:18180/stats','pod_uid':'uid','container':'t'}])
        data=src.npu_gates();self.assertEqual(urls,['http://127.0.0.1:18180/stats'])
        self.assertNotIn('private',json.dumps(data))
        with self.assertRaises(ValueError):ReadOnlySources('n',gate_endpoints=[{'url':'http://127.0.0.1:8090/feeder/occupancy'}]).npu_gates()

    def test_pending_requests_are_visible_without_claiming_applied_resources(self):
        pod={'metadata':{'uid':'uid','name':'waiting','annotations':{'ketris.re.kr/time-weight':'3'}},'spec':{'containers':[{'name':'work','resources':{'limits':{'keti.re.kr/mig-test-share':50}}}]},'status':{'phase':'Pending'}}
        kube=self.cache('kubernetes',{'pods':[pod],'checkpoint':{'Data':{'PodDeviceEntries':[]}}})
        o=NodeObserver('n',{'kubernetes':kube});s=o.collect_node()
        self.assertEqual(len(s.workloads),1);w=s.workloads[0]
        self.assertEqual(w.requested['space_percent'].value,50)
        self.assertEqual(w.requested['time_weight'].value,3)
        self.assertIsNone(w.applied['mps_percent'].value)
        self.assertIsNone(w.observed['charged_share'].value)
        self.assertEqual(w.state,'Pending')

    def test_optional_empty_sources_do_not_claim_runtime_is_connected(self):
        for name in ['runtime','npu_gates']:
            c=self.cache(name,[])
            self.assertEqual(c.read()[1].status,'unavailable')
            self.assertIsNotNone(c.read()[1].reason)

    def test_failed_or_expired_empty_runtime_is_stale_instead_of_confirmed_absence(self):
        for name in ['runtime','npu_gates']:
            c=self.cache(name,[])
            c.provider=lambda:(_ for _ in ()).throw(RuntimeError())
            c.poll()
            self.assertEqual(c.read()[1].status,'stale')
            c=self.cache(name,[]);c.last_success_time=time.monotonic()-100
            self.assertEqual(c.read()[1].status,'stale')

    def test_complete_mig_window_and_partial_freshness(self):
        with tempfile.TemporaryDirectory() as root:
            a=Path(root)/'a';b=Path(root)/'b';a.mkdir();b.mkdir()
            for d in [a,b]:(d/'feed.log').write_text('total=0 kernels=0\n')
            state={'schema_version':1,'node':'n','gpu_uuid':'GPU-895722c8-b706-27ae-018d-c43e405e6c35','mig_uuid':'MIG-5ffbbadf-e256-59fa-a186-40452bbfdfcf','generation':'g','budget_domain':'mig:g','inventory':{'profile':'1g'},'observed_at':'2026-10-13T00:00:00Z','manager_state':'running','tenants':[dict(allocation_id=name,pod_uid=name,container='c',phase='armed',space_percent=50,time_weight=1,runtime_generation='g',control_dir=str(d)) for name,d in [('a',a),('b',b)]]}
            rt=self.cache('runtime',[state]);o=NodeObserver('n',self.runtime_sources(rt,state))
            o.collect_node();(a/'feed.log').write_text('total=30 kernels=3\n');(b/'feed.log').write_text('total=10 kernels=1\n')
            s=o.collect_node();self.assertEqual([w.observed['charged_share'].value for w in s.workloads],[.75,.25])
            os.utime(b/'feed.log',(time.time()-40,time.time()-40))
            self.assertTrue(all(w.observed['charged_share'].value is None for w in o.collect_node().workloads))
