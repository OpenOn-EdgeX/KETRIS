#!/usr/bin/env python3
"""Scoped real K8s/MIG experiment with finally restoration. Never resets the GPU."""
import argparse,json,os,re,shutil,socket,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
from ketris_mig.contracts import ShareRequest
from ketris_mig.inventory import read_inventory
from ketris_mig.manifest import build_manifest
from ketris_mig.evidence import validate_measurement
from ketris_mig.admission import mig_admission_manifest
ROOT=Path(__file__).resolve().parents[2]
NODE='gpu-npu-server-02';NS='ketris-mig-runtime-verify';STATE=Path('/var/lib/ketris-mig')

class Experiment:
    def __init__(self,out,duration=20,repeats=3,lifecycle_only=False):
        self.out=out.resolve();self.out.mkdir(parents=True);self.rows=[];self.duration=duration;self.repeats=repeats;self.lifecycle_only=lifecycle_only
    def save(self,name,value):
        p=self.out/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,indent=2,allow_nan=False))
    def cmd(self,args,**kwargs):
        r=subprocess.run(args,capture_output=True,text=True,timeout=kwargs.pop('timeout',150),**kwargs)
        with (self.out/'commands.log').open('a') as f:f.write('$ '+' '.join(args)+'\n'+r.stdout+r.stderr)
        if r.returncode:raise RuntimeError('command failed: '+' '.join(args)+' '+r.stderr[-1500:])
        return r.stdout
    def get(self,kind,name=None,ns=None):
        args=['kubectl','get',kind]+([name] if name else [])+(['-n',ns] if ns else ['-A'] if kind=='pods' else [])+['-o','json']
        return json.loads(self.cmd(args))
    def apply(self,name,value):
        self.save(name,value);self.cmd(['kubectl','apply','-f',str(self.out/name)])
    def wait(self,fn,seconds=150):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if fn():return
            time.sleep(.25)
        raise TimeoutError('condition timed out')
    def send(self,path,msg):
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s:s.sendto(msg.encode(),str(path))
    def binding(self,pod_uid):
        for path in (STATE/'bindings').glob('*.json'):
            value=json.loads(path.read_text())
            if value.get('pod_uid')==pod_uid and value.get('phase')=='bound':return value
    def await_binding(self,name,uid):
        def ready():
            pod=self.get('pod',name,NS)
            if pod.get('status',{}).get('phase')=='Failed':
                self.save('failures/'+name+'-status.json',pod['status'])
                raise RuntimeError('Pod failed: '+pod['status'].get('message',''))
            return self.binding(uid) is not None
        self.wait(ready)
    def stats(self,b):
        self.send(b['socket'],'time_stats');time.sleep(.08)
        tail=(Path(b['control_dir'])/'feed.log').read_text()[-4096:]
        m=re.findall(r'total=(\d+) kernels=(\d+) credit=(-?\d+)',tail)[-1]
        return dict(zip(['charged_us','kernels','credit_us'],map(int,m)))
    def probe(self,entry,baseline):
        label='baseline-start' if baseline else 'gated-start';d=self.out/label;d.mkdir()
        env=os.environ.copy();env.update(CUDA_VISIBLE_DEVICES=entry.mig_uuid,CUDA_MPS_PIPE_DIRECTORY=self.manager_pipe(entry),CUDA_MPS_ACTIVE_THREAD_PERCENTAGE='100',
           LD_PRELOAD=str(ROOT/'.build/runtime'/('libbless-baseline.so' if baseline else 'libbless.so')),LD_LIBRARY_PATH='/usr/local/cuda-12.9/lib64',BLESS_TIME_START_GATED='1',BLESS_TIME_WC='0',BLESS_SPACE_MODE='mps_env',BLESS_STATS_LOG=str(d/'feed.log'),KETRIS_DEVICE_INFO_LOG=str(d/'device.json'),KETRIS_PROBE_HOLD='1')
        with (d/'stdout.log').open('w') as log:
            p=subprocess.Popen([str(ROOT/'.build/runtime/runtime_start_probe')],env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                self.wait(lambda:'before_launch' in (d/'stdout.log').read_text() or p.poll() is not None,30)
                assert p.poll() is None,(d/'stdout.log').read_text()
                time.sleep(1)
                before=(d/'stdout.log').read_text();complete='launch_complete' in before
                assert complete==baseline,before
                if not baseline:
                    info=json.loads((d/'device.json').read_text());assert info['actual_cuda_uuid']==entry.mig_uuid,info
                    self.send('/tmp/bless-'+str(p.pid)+'.sock','time_add 10000')
                    self.wait(lambda:'launch_complete' in (d/'stdout.log').read_text(),10)
                self.save(label+'/assertions.json',{'completed_without_credit':complete,'completed_after_credit':True,'baseline':baseline})
            finally:p.terminate();p.wait(10);Path('/tmp/bless-'+str(p.pid)+'.sock').unlink(missing_ok=True)
    def manager_pipe(self,entry):
        key=entry.resource_name.split('/')[-1]
        config=json.loads((STATE/'plugins'/(key+'-'+entry.generation+'.json')).read_text())
        if (config.get('mig_uuid'),config.get('generation'),config.get('active'))!=(entry.mig_uuid,entry.generation,True):
            raise ValueError('MIG probe requires the active current-generation plugin')
        return config['pipe_dir']
    def independence(self,inventory):
        pods=[];records=[]
        try:
            for i,entry in enumerate(inventory):
                pod=build_manifest(ShareRequest(entry.mig_uuid,100,3),'ketris-device-plugin:mig-20261013-v9',NS,['/opt/ketris/runtime_start_probe'],inventory)
                name='domain-'+str(i);pod['metadata']['name']=name;pod['spec']['nodeSelector']={'kubernetes.io/hostname':NODE};pod['spec']['terminationGracePeriodSeconds']=5
                pod['spec']['containers'][0]['env'].append({'name':'KETRIS_PROBE_HOLD','value':'1'})
                self.apply('independence/'+name+'.json',pod);pods.append(name)
                uid=self.get('pod',name,NS)['metadata']['uid'];self.await_binding(name,uid)
                binding=self.binding(uid)
                self.wait(lambda:'launch_complete' in self.cmd(['kubectl','logs',name,'-n',NS]),15)
                records.append(binding)
            time.sleep(.5)
            exports=[]
            for entry in inventory:
                value=json.loads((STATE/'partitions'/(entry.resource_name.split('/')[-1]+'.json')).read_text())
                assert len(value['tenants'])==1 and value['tenants'][0]['budget_us']==10000,value
                assert value['tenants'][0]['actual_cuda_uuid']==entry.mig_uuid,value
                exports.append(value)
            assert records[0]['socket']!=records[1]['socket']
            assert self.manager_pipe(inventory[0])!=self.manager_pipe(inventory[1])
            self.save('independence/result.json',{'independent_budget_domains':True,'same_container_name':'workload','records':records,'exports':exports})
        finally:
            for name in pods:self.cmd(['kubectl','delete','pod',name,'-n',NS,'--ignore-not-found','--timeout=60s'])
            time.sleep(3)
    def measure(self,entry,label,spaces,weights,unlimited=False):
        d=self.out/label;d.mkdir();pods=[];bindings={}
        self.save(label+'/input.json',{'origin':'MOCK_SKku_REQUEST_NOT_INSTITUTION_NETWORK','mig_uuid':entry.mig_uuid,'gpu_uuid':entry.gpu_uuid,'space_percent':spaces,'time_weights':weights,'time_policy':'unlimited_calibration' if unlimited else 'runtime_weighted'})
        try:
            for i,(space,weight) in enumerate(zip(spaces,weights)):
                name=chr(65+i);podname='measure-'+name.lower()
                pod=build_manifest(ShareRequest(entry.mig_uuid,space,weight),'ketris-device-plugin:mig-20261013-v9',NS,['python3','/lab/worker.py'],[entry])
                pod['metadata']['name']=podname;pod['spec']['nodeSelector']={'kubernetes.io/hostname':NODE}
                pod['spec']['terminationGracePeriodSeconds']=5
                pod['spec']['volumes']=[{'name':'lab','hostPath':{'path':str(self.out),'type':'Directory'}}]
                c=pod['spec']['containers'][0];c['volumeMounts']=[{'name':'lab','mountPath':'/lab'}]
                c['env'] += [{'name':'MEASURE_DIR','value':'/lab/'+label},{'name':'MEASURE_NAME','value':name}]
                c['env'] += [{'name':'MEASURE_SECONDS','value':str(self.duration)},{'name':'MEASURE_REPEATS','value':str(self.repeats)}]
                c['resources']['limits']['memory']='512Mi';c['resources']['requests'].update(cpu='200m',memory='128Mi')
                self.apply(label+'/'+podname+'.json',pod);pods.append(podname)
                uid=self.get('pod',podname,NS)['metadata']['uid']
                self.await_binding(podname,uid)
                bindings[name]=self.binding(uid)
                if unlimited:self.send(bindings[name]['socket'],'time_credit -1')
            self.wait(lambda:all((d/('ready-'+n)).exists() for n in bindings))
            self.save(label+'/bindings.json',bindings)
            for repeat in range(1,self.repeats+1):
                if not unlimited:
                    for b in bindings.values():self.send(b['socket'],'time_credit 0')
                before={n:self.stats(b) for n,b in bindings.items()};(d/f'go-{repeat}').touch()
                self.wait(lambda:all((d/f'result-{n}-{repeat}.json').exists() for n in bindings),45)
                after={n:self.stats(b) for n,b in bindings.items()};workers={}
                for n,b in bindings.items():
                    w=json.loads((d/f'result-{n}-{repeat}.json').read_text());info=json.loads((Path(b['control_dir'])/'device.json').read_text())
                    w.update(actual_cuda_uuid=info['actual_cuda_uuid'],charged_us=after[n]['charged_us']-before[n]['charged_us'],kernels=after[n]['kernels']-before[n]['kernels'],before=before[n],after=after[n],pod_uid=b['pod_uid'])
                    w['iterations_per_second']=w['iterations']/w['elapsed_s'];workers[n]=w
                row={'config':label,'repeat':repeat,'mig_uuid':entry.mig_uuid,'workers':workers,'time_policy':'unlimited_calibration' if unlimited else 'runtime_weighted'}
                validate_measurement(row);self.rows.append(row);self.save(label+f'/measurement-{repeat}.json',row)
                print(json.dumps({'config':label,'repeat':repeat,'workers':workers}),flush=True)
            self.save(label+'/partition-export.json',json.loads((STATE/'partitions'/(entry.resource_name.split('/')[-1]+'.json')).read_text()))
            if label=='combined50_50_time3_1':self.restart_check(entry,bindings)
        finally:
            (d/'stop').touch()
            for name in pods:
                try:
                    (d/(name+'.log')).write_text(self.cmd(['kubectl','logs',name,'-n',NS]))
                    self.cmd(['kubectl','delete','pod',name,'-n',NS,'--ignore-not-found','--timeout=30s'])
                except Exception as e:print('cleanup warning',e,flush=True)
            time.sleep(3)
    def restart_check(self,entry,bindings):
        (self.out/'combined50_50_time3_1'/'resume').touch()
        before={n:self.stats(b) for n,b in bindings.items()}
        owner=list((Path(self.manager_pipe(entry)).parent).glob('owner.json'))[0]
        mps_pid=json.loads(owner.read_text())['pid']
        self.cmd(['kubectl','rollout','restart','ds/ketris-mig-runtime','-n','kube-system'])
        self.cmd(['kubectl','rollout','status','ds/ketris-mig-runtime','-n','kube-system','--timeout=120s'])
        self.wait(lambda:all(self.binding(b['pod_uid']) for b in bindings.values()))
        self.wait(lambda:all(self.stats(b)['kernels']>before[n]['kernels'] for n,b in bindings.items()),20)
        assert json.loads(owner.read_text())['pid']==mps_pid
        self.save('restart.json',{'mps_pid_preserved':True,'actual_pid':mps_pid,'bindings_restored':True,'kernel_progress':True})
    def run(self):
        gpu=self.cmd(['nvidia-smi','--query-gpu=uuid,mig.mode.current','--format=csv,noheader']).strip();assert gpu.endswith('Disabled'),gpu
        apps=self.cmd(['nvidia-smi','--query-compute-apps=pid,process_name','--format=csv,noheader'])
        assert all('mps-server' in l for l in apps.splitlines() if l.strip()),apps
        pods=self.get('pods')
        for p in pods['items']:
            if p.get('status',{}).get('phase') not in ['Pending','Running']:continue
            for c in p['spec']['containers']:
                limits=c.get('resources',{}).get('limits',{})
                assert not any(k.startswith(('keti.re.kr/lsu','keti.re.kr/mig-','nvidia.com/')) for k in limits),(p['metadata']['name'],limits)
        self.save('node-before.json',self.get('node',NODE));self.save('core-before.json',self.get('ds','ketris-device-plugin-lsu','kube-system'));self.save('webhook-before.json',self.get('deploy','ketris-webhook','kube-system'))
        node=self.get('node',NODE);oldlabel=node['metadata'].get('labels',{}).get('ketris.re.kr/mig-runtime')
        cdi=Path('/var/run/cdi/nvidia.yaml');backup=cdi.read_bytes();(self.out/'cdi-before.yaml').write_bytes(backup)
        active=self.cmd(['systemctl','is-active','nvidia-mps']).strip()=='active';enabled=False;created=[];restoration=[]
        if active:
            env=os.environ.copy();env['CUDA_MPS_PIPE_DIRECTORY']='/tmp/nvidia-mps'
            servers=self.cmd(['nvidia-cuda-mps-control'],input='get_server_list\n',env=env).split()
            for pid in servers:
                clients=self.cmd(['nvidia-cuda-mps-control'],input='get_client_list '+pid+'\n',env=env).strip()
                assert not clients,'baseline MPS has active clients: '+clients
        try:
            self.cmd(['systemctl','stop','nvidia-mps'])
            self.cmd(['nvidia-smi','-i','0','-mig','1']);enabled=True
            self.cmd(['nvidia-smi','mig','-i','0','-cgi','14,14','-C'])
            inventory=read_inventory();created=[e.gi_id for e in inventory];assert len(inventory)==2
            self.save('inventory.json',[e.to_dict() for e in inventory])
            self.cmd(['nvidia-ctk','cdi','generate','--output',str(cdi)])
            for e in inventory:assert e.mig_uuid in cdi.read_text()
            self.cmd(['bash',str(ROOT/'mig-sharing/tools/deploy.sh'),'--apply'])
            self.cmd(['kubectl','patch','cm','ketris-mig-config','-n','kube-system','--type=merge','-p',json.dumps({'data':{'allowlist':','.join(e.mig_uuid for e in inventory)}})])
            self.cmd(['kubectl','label','node',NODE,'ketris.re.kr/mig-runtime=true','--overwrite'])
            self.cmd(['kubectl','set','image','ds/ketris-device-plugin-lsu','-n','kube-system','plugin=ketris-device-plugin:mig-20261013-v9'])
            self.cmd(['kubectl','rollout','restart','ds/ketris-device-plugin-lsu','-n','kube-system'])
            self.cmd(['kubectl','set','image','deploy/ketris-webhook','-n','kube-system','webhook=ketris-device-plugin:mig-20261013-v9'])
            for resource in ['ds/ketris-mig-runtime','ds/ketris-device-plugin-lsu','deploy/ketris-webhook']:
                self.cmd(['kubectl','rollout','status',resource,'-n','kube-system','--timeout=120s'])
            # Fail closed only for MIG requests; legacy shared-cluster policy remains unchanged.
            legacy=self.get('mutatingwebhookconfiguration','ketris-env-webhook')
            self.apply('mig-admission.json',mig_admission_manifest(legacy))
            def advertised():
                a=self.get('node',NODE)['status']['allocatable']
                return all(a.get(e.resource_name)=='200' for e in inventory) and int(a.get('keti.re.kr/lsu','0'))==0 and a.get('keti.re.kr/npu-pe')=='8'
            self.wait(advertised)
            alloc=self.get('node',NODE)['status']['allocatable'];assert int(alloc.get('keti.re.kr/lsu','0'))==0,alloc;assert alloc['keti.re.kr/npu-pe']=='8',alloc
            self.save('node-mig.json',self.get('node',NODE))
            self.cmd(['kubectl','create','namespace',NS])
            self.probe(inventory[0],True);self.probe(inventory[0],False)
            self.independence(inventory)
            invalid=build_manifest(ShareRequest(inventory[0].mig_uuid,50,3),'ketris-device-plugin:mig-20261013-v9',NS,['sleep','5'],inventory)
            invalid['spec']['containers'][0]['env'].append({'name':'CUDA_VISIBLE_DEVICES','value':'0'})
            self.save('invalid-override.json',invalid)
            r=subprocess.run(['kubectl','apply','--dry-run=server','-f',str(self.out/'invalid-override.json')],capture_output=True,text=True)
            assert r.returncode!=0,r.stdout;self.save('admission-negative.json',{'rejected':True,'message':r.stderr})
            shutil.copy(ROOT/'mig-sharing/tools/worker.py',self.out/'worker.py')
            source=ROOT/'mig-sharing/tests/native/gemm.cu'
            self.cmd(['/usr/local/cuda-12.9/bin/nvcc','--cudart','shared','-shared','-Xcompiler','-fPIC','-O2','-arch=sm_120',str(source),'-o',str(self.out/'gemm.so')])
            configs=[('solo100',[100],[1],True),('space50_50',[50,50],[1,1],True),('time3_1',[100,100],[3,1],False),('combined50_50_time3_1',[50,50],[3,1],False)]
            if self.lifecycle_only:configs=configs[-1:]
            for label,s,w,u in configs:
                print('START '+label,flush=True);self.measure(inventory[0],label,s,w,u)
            self.save('measurements.json',self.rows)
            summary={}
            for config in set(r['config'] for r in self.rows):
                rows=[r for r in self.rows if r['config']==config]
                shares=[r['workers']['A']['charged_us']/sum(w['charged_us'] for w in r['workers'].values()) for r in rows]
                mean=sum(shares)/len(shares)
                summary[config]={'repeats':len(rows),'mean_A_charged_share':mean,'within_1pp_of_75':abs(mean-.75)<=.01 if '3_1' in config else None}
            self.save('summary.json',summary)
        except BaseException as error:
            self.save('failure.json',{'type':type(error).__name__,'message':str(error)})
            try:self.save('failure-pods.json',self.get('pods',ns=NS))
            except Exception:pass
            try:(self.out/'failure-runtime.log').write_text(self.cmd(['kubectl','logs','-n','kube-system','-l','app=ketris-mig-runtime','-c','runtime','--tail=40']))
            except Exception:pass
            try:self.save('failure-bindings.json',[json.loads(p.read_text()) for p in (STATE/'bindings').glob('*.json')])
            except Exception:pass
            raise
        finally:
            def restore(label,fn):
                try:fn();restoration.append({'action':label,'ok':True})
                except Exception as e:restoration.append({'action':label,'ok':False,'error':str(e)})
            restore('delete experiment pods',lambda:self.cmd(['kubectl','delete','namespace',NS,'--ignore-not-found','--timeout=90s']))
            restore('empty managed allowlist',lambda:self.cmd(['kubectl','patch','cm','ketris-mig-config','-n','kube-system','--type=merge','-p','{"data":{"allowlist":""}}']))
            restore('stop runtime reconciliation',lambda:self.cmd(['kubectl','label','node',NODE,'ketris.re.kr/mig-runtime-']))
            restore('wait runtime pod deletion',lambda:self.cmd(['kubectl','wait','--for=delete','pod','-n','kube-system','-l','app=ketris-mig-runtime','--timeout=90s']))
            for owner in (STATE/'mps').glob('*/owner.json'):
                value=json.loads(owner.read_text())
                if value.get('mig_uuid') in [e.mig_uuid for e in locals().get('inventory',[])]:
                    load=self.cmd(['systemctl','show',value['unit'],'--property=LoadState','--value']).strip()
                    if load!='not-found':restore('stop own '+value['unit'],lambda v=value:self.cmd(['systemctl','stop',v['unit']]))
            for gi in created:
                restore('delete own CI '+str(gi),lambda g=gi:self.cmd(['nvidia-smi','mig','-i','0','-gi',str(g),'-dci']))
                restore('delete own GI '+str(gi),lambda g=gi:self.cmd(['nvidia-smi','mig','-i','0','-gi',str(g),'-dgi']))
            if enabled:restore('restore MIG disabled',lambda:self.cmd(['nvidia-smi','-i','0','-mig','0']))
            restore('restore CDI',lambda:cdi.write_bytes(backup))
            if active:restore('restore baseline MPS',lambda:self.cmd(['systemctl','start','nvidia-mps']))
            # Current core plugin discovers hardware at startup; restart after restoring geometry.
            restore('refresh core inventory',lambda:self.cmd(['kubectl','rollout','restart','ds/ketris-device-plugin-lsu','-n','kube-system']))
            restore('wait core healthy',lambda:self.cmd(['kubectl','rollout','status','ds/ketris-device-plugin-lsu','-n','kube-system','--timeout=120s']))
            if oldlabel is not None:restore('restore node label',lambda:self.cmd(['kubectl','label','node',NODE,'ketris.re.kr/mig-runtime='+oldlabel,'--overwrite']))
            self.save('restoration.json',restoration);self.save('node-after.json',self.get('node',NODE))
            (self.out/'after.txt').write_text(self.cmd(['nvidia-smi']))
            assert all(v['ok'] for v in restoration),restoration

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default=str(ROOT/'reports'/('ketris_mig_runtime_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))));p.add_argument('--lifecycle-only',action='store_true');p.add_argument('--seconds',type=int,choices=range(1,61));a=p.parse_args()
    Experiment(Path(a.out),duration=a.seconds or (3 if a.lifecycle_only else 20),repeats=1 if a.lifecycle_only else 3,lifecycle_only=a.lifecycle_only).run()
if __name__=='__main__':main()
