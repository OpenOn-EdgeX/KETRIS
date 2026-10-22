"""Operator E2E driver. Only own namespaces/CQ/Jobs are mutated; no native IPC."""
import argparse,base64,hashlib,json,os,subprocess,time
from pathlib import Path
NS='ketris-integration';WNS='ketris-integration-workloads';OWNER='ketris-institution-bridge';CQ='ketris-integration-mig';IMAGE='ketris-institution-bridge:20261015-v5'
def kub(args,body=None):
 r=subprocess.run(['kubectl']+args,input=json.dumps(body) if body is not None else None,text=True,capture_output=True,timeout=200 if args[:2]==['rollout','status'] else 45)
 if r.returncode:raise RuntimeError('kubectl '+str(args[:3])+': '+r.stderr[-1500:])
 return r.stdout
def get(kind,ns=None,name=None):return json.loads(kub(['get',kind]+([name] if name else [])+(['-n',ns] if ns else [])+['-o','json']))
def save(root,name,data):
 (root/name).write_text(data if isinstance(data,str) else json.dumps(data,indent=2));return data
def wait(fn,why,seconds=180):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  result=fn()
  if result:return result
  time.sleep(1)
 raise TimeoutError(why)
def apply(d):return kub(['apply','--server-side','--field-manager=ketris-institution-e2e','-f','-'],d)
def quota(value):return kub(['patch','clusterqueue',CQ,'--type=json','-p',json.dumps([dict(op='replace',path='/spec/resourceGroups/0/flavors/0/resources/2/nominalQuota',value=str(value))])])
def job(name,args,config=None):
 volumes=[dict(name='auth',secret={'secretName':'bridge-auth','defaultMode':256}),dict(name='tmp',emptyDir={}),dict(name='target',configMap={'name':'live-mig-target'})]
 mounts=[dict(name='auth',mountPath='/auth',readOnly=True),dict(name='tmp',mountPath='/tmp'),dict(name='target',mountPath='/inputs',readOnly=True)]
 if config:volumes.append(dict(name='scenario',configMap={'name':config}));mounts.append(dict(name='scenario',mountPath='/scenario',readOnly=True))
 return dict(apiVersion='batch/v1',kind='Job',metadata=dict(name=name,namespace=NS,labels={'app.kubernetes.io/managed-by':OWNER}),spec=dict(backoffLimit=0,activeDeadlineSeconds=180,template=dict(metadata=dict(labels={'app':name}),spec=dict(restartPolicy='Never',containers=[dict(name='client',image=IMAGE,imagePullPolicy='Never',command=['python3']+args,env=[dict(name='RUN_ID',value=RUN),dict(name='POD_NAME',valueFrom={'fieldRef':{'fieldPath':'metadata.name'}}),dict(name='POD_UID',valueFrom={'fieldRef':{'fieldPath':'metadata.uid'}})],volumeMounts=mounts,resources=dict(requests={'cpu':'50m','memory':'64Mi'},limits={'cpu':'500m','memory':'256Mi'}))],volumes=volumes))))
def run_client(root,name,args,config=None):
 apply(job(name,args,config));wait(lambda:(j:=get('job',NS,name)).get('status',{}).get('succeeded') or (_failed(j,name)),name,180)
 log=kub(['logs','-n',NS,'job/'+name]);save(root,'client-'+name+'.log',log);save(root,'client-'+name+'-job.json',get('job',NS,name));save(root,'client-'+name+'-pods.json',json.loads(kub(['get','pods','-n',NS,'-l','job-name='+name,'-o','json'])));return log
def _failed(j,name):
 if j.get('status',{}).get('failed'):raise RuntimeError('client failed: '+name)
 return False
def cli(root,role,wid,trace,weight=3):
 args=['-m','ketris_integration.client','--workload',wid,'--trace',trace]
 if role=='skku':args+=['--target-file','/inputs/target.json','--weight',str(weight)]
 return run_client(root,role+'-'+wid,args+[role+'-demo' if role=='yonsei' else 'skku-demo'])
def workload_for(wid):
 for w in get('workloads',WNS)['items']:
  if any(ps['template']['metadata'].get('annotations',{}).get('ketris.integration/workload-id')==wid for ps in w['spec']['podSets']):return w
 return None
def by_owner(pods,uid):return [p for p in pods if any(o.get('uid')==uid for o in p['metadata'].get('ownerReferences',[]))]
def job_for(w):
 owner=next(o for o in w['metadata']['ownerReferences'] if o['kind']=='Job');return get('job',WNS,owner['name'])
def state(wid):
 out=kub(['exec','-n',NS,'deploy/'+OWNER,'--','python3','-m','ketris_integration.client','--workload',wid,'state'])
 rows=[json.loads(line) for line in out.splitlines() if line.startswith('{')];return next(x['response'] for x in rows if x.get('event')=='rpc_response')
def checks(w):return {a['name']:a for a in w.get('status',{}).get('admissionChecks',[])}
def cond(w,key):return any(c['type']==key and c['status']=='True' for c in w.get('status',{}).get('conditions',[]))
def select_guard_rejection(text):
 decoder=json.JSONDecoder();pos=0
 while pos<len(text):
  while pos<len(text) and text[pos].isspace():pos+=1
  try:event,end=decoder.raw_decode(text,pos)
  except ValueError:break
  pos=end;w=event.get('object',event);ac=checks(w)
  if ac.get('keti-scheduler',{}).get('state')=='Ready' and ac.get('ketris-plan-validation',{}).get('state')=='Rejected':return w
 return None
def cpu(root):
 quota(0);negative_name='negative-'+RUN;cm=dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='negative-scenario-'+RUN,namespace=NS,labels={'app.kubernetes.io/managed-by':OWNER}),data={'negative_rpc.py':Path(__file__).with_name('negative_rpc.py').read_text()});apply(cm)
 if not (root/'negative-jobs-before.json').exists():save(root,'negative-jobs-before.json',get('jobs',WNS))
 run_client(root,negative_name,['/scenario/negative_rpc.py'],cm['metadata']['name'])
 if not (root/'negative-jobs.json').exists():save(root,'negative-jobs.json',get('jobs',WNS))
 before_ids={j['metadata']['uid'] for j in json.loads((root/'negative-jobs-before.json').read_text())['items']}
 after_ids={j['metadata']['uid'] for j in json.loads((root/'negative-jobs.json').read_text())['items']}
 assert before_ids==after_ids,'negative RPCs created a Job'
 wid=RUN+'-probe-stream';cli(root,'yonsei',wid,'trace-'+RUN);cli(root,'skku',wid,'trace-'+RUN)
 w=wait(lambda:workload_for(wid),'probe Workload');assert not w.get('status',{}).get('admission')
 stream=(root/'guard-watch.jsonstream').open('w');watch_error=(root/'guard-watch.stderr').open('w')
 watch_url='/apis/kueue.x-k8s.io/v1beta2/namespaces/'+WNS+'/workloads?watch=true&resourceVersion='+w['metadata']['resourceVersion']+'&fieldSelector=metadata.name%3D'+w['metadata']['name']
 watcher=subprocess.Popen(['kubectl','get','--raw',watch_url],stdout=stream,stderr=watch_error)
 patch=[dict(op='replace',path='/spec/podSets/0/template/metadata/annotations/ketris.re.kr~1time-weight',value='2')]
 save(root,'operator-fault-injection.json',dict(workload_uid=w['metadata']['uid'],patch=patch,reason='controlled annotation drift before quota reservation'))
 save(root,'guard-probe-mutated.json',json.loads(kub(['patch','workload',w['metadata']['name'],'-n',WNS,'--type=json','-p',json.dumps(patch),'-o','json'])))
 quota(100)
 def rejected():
  if watcher.poll() is not None:raise RuntimeError('watch terminated: '+(root/'guard-watch.stderr').read_text())
  return select_guard_rejection((root/'guard-watch.jsonstream').read_text())
 try:w=wait(rejected,'real KETI Ready + own guard Rejected',180)
 finally:watcher.terminate();watcher.wait(timeout=10);stream.close();watch_error.close()
 j=job_for(w);pods=by_owner(get('pods',WNS)['items'],j['metadata']['uid']);assert not pods and j['spec']['suspend'] and not cond(w,'Admitted');save(root,'guard-rejected.json',dict(workload=w,job=j,pods=pods));quota(0)
 wait(lambda:not workload_for(wid).get('status',{}).get('admission'),'rejected quota release',120)
 for letter,weight in [('a',3),('b',1)]:
  wid=RUN+'-'+letter;cli(root,'yonsei',wid,'trace-'+RUN,weight);cli(root,'skku',wid,'trace-'+RUN,weight);wait(lambda:workload_for(wid),wid)
 jobs=get('jobs',WNS);pods=get('pods',WNS);workloads=get('workloads',WNS)
 for letter in ('a','b'):
  w=workload_for(RUN+'-'+letter);j=job_for(w);assert j['spec']['suspend'] and not by_owner(pods['items'],j['metadata']['uid']) and not w.get('status',{}).get('admission')
 save(root,'queue-held.json',dict(jobs=jobs,pods=pods,workloads=workloads));print('CPU_AND_NEGATIVE_COMPLETE',flush=True)
def partition(key):return json.loads((Path('/var/lib/ketris-mig/partitions')/(key.split('/')[-1]+'.json')).read_text())
def all_partitions():return [json.loads(p.read_text()) for p in Path('/var/lib/ketris-mig/partitions').glob('*.json')]
def capture_dashboard(root):
 import urllib.request
 with urllib.request.urlopen('http://127.0.0.1:31080/api/v1/nodes/devmaster/snapshot',timeout=10) as response:save(root,'dashboard-active.json',json.loads(response.read()))
def hardware(root):
 from preflight import preflight,Reader
 before=preflight(Reader());save(root,'preflight-before-hardware.json',before.__dict__)
 if not before.ok:raise RuntimeError('GPU busy; no quota released: '+str(before.active_gpu_pods))
 assert before.target==json.loads((root/'target.json').read_text()),'target changed'
 quota(100);target=before.target
 def both():
  states=[state(RUN+'-'+x) for x in ('a','b')]
  return states if all(x['state']=='EXECUTION_OBSERVED' for x in states) else None
 states=wait(both,'both real CUDA execution evidence',120)
 save(root,'states-executing.json',states);save(root,'workloads-admitted.json',get('workloads',WNS));save(root,'jobs-admitted.json',get('jobs',WNS));save(root,'pods-admitted.json',get('pods',WNS));time.sleep(5)
 capture_dashboard(root);save(root,'all-partitions-active.json',all_partitions());windows=[]
 for i in range(3):
  all_start=all_partitions();start=next(p for p in all_start if p['mig_uuid']==target['mig_uuid']);end_time=time.monotonic()+20
  while time.monotonic()<end_time:time.sleep(min(.5,end_time-time.monotonic()))
  from datetime import datetime
  end=partition(target['resource_name'])
  while (datetime.fromisoformat(end['observed_at'])-datetime.fromisoformat(start['observed_at'])).total_seconds()<20:
   time.sleep(.1);end=partition(target['resource_name'])
  all_end=all_partitions();windows.append(dict(repeat=i+1,start=start,end=end,all_start=all_start,all_end=all_end));save(root,'windows.json',windows);print('MEASURED_WINDOW',i+1,flush=True)
 wait(lambda:all(state(RUN+'-'+x)['state']=='COMPLETED' for x in ('a','b')),'normal completion',180)
 for letter in ('a','b'):save(root,'state-'+letter+'-final.json',state(RUN+'-'+letter))
 for kind in ('jobs','pods','workloads'):save(root,kind+'-final.json',get(kind,WNS))
 for letter in ('a','b'):
  j=job_for(workload_for(RUN+'-'+letter));save(root,'worker-'+letter+'.log',kub(['logs','-n',WNS,'job/'+j['metadata']['name']]))
 save(root,'partition-after.json',partition(target['resource_name']));save(root,'postflight.json',preflight(Reader()).__dict__);save(root,'mig-mode.txt',subprocess.check_output(['nvidia-smi','--query-gpu=uuid,mig.mode.current','--format=csv,noheader'],text=True));print('HARDWARE_COMPLETE',flush=True)
def finalize(root):
 old=[state(RUN+'-'+x) for x in ('a','b')];save(root,'state-before-restart.json',old);kub(['rollout','restart','deployment/'+OWNER,'-n',NS]);kub(['rollout','status','deployment/'+OWNER,'-n',NS,'--timeout=180s'])
 for letter in ('a','b'):
  wid=RUN+'-'+letter;log=(root/('client-yonsei-'+wid+'.log')).read_text();req=next(json.loads(line)['request'] for line in log.splitlines() if line.startswith('{') and json.loads(line).get('event')=='rpc_request')
  cmname='replay-'+wid;apply(dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name=cmname,namespace=NS,labels={'app.kubernetes.io/managed-by':OWNER}),data={'request.json':json.dumps(req)}));run_client(root,'duplicate-'+wid,['-m','ketris_integration.client','--file','/scenario/request.json','push-profile'],cmname)
 new=[state(RUN+'-'+x) for x in ('a','b')];save(root,'state-after-restart.json',new);assert [x['job_uid'] for x in old]==[x['job_uid'] for x in new];save(root,'bridge.log',kub(['logs','-n',NS,'deploy/'+OWNER]));print('DURABLE_REPLAY_COMPLETE',flush=True)
def main():
 global RUN
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--run-id',default='institution-grpc-20261015-01');p.add_argument('--phase',choices=['cpu','hardware','finalize'],required=True);a=p.parse_args();RUN=a.run_id;a.root.mkdir(parents=True,exist_ok=True);save(a.root,'run-info.json',dict(run_id=RUN,target=json.loads((a.root/'target.json').read_text()),mode='SYNTHETIC_INPUT_ACTUAL_RPC_AND_CUDA',client_image=IMAGE));{'cpu':cpu,'hardware':hardware,'finalize':finalize}[a.phase](a.root)
if __name__=='__main__':main()
