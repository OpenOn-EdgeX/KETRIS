"""Read-only final environment capture and experiment-watch shutdown."""
import json,pathlib,subprocess,time
P=pathlib.Path(__file__).resolve().parent
def capture(name,args):
 p=subprocess.run(args,text=True,capture_output=True,timeout=30)
 (P/name).write_text(p.stdout)
 if p.returncode:raise RuntimeError(p.stderr)
 return p.stdout
def keyed(name):
 return {(x['kind'],x['metadata'].get('namespace',''),x['metadata']['name']):{'uid':x['metadata']['uid'],'spec':x['spec']} for x in json.loads((P/name).read_text())['items']}
capture('operating-after.json',['kubectl','get','deployments,daemonsets,statefulsets','-A','-o','json'])
capture('queues-after.json',['kubectl','get','clusterqueues,localqueues,resourceflavors','-A','-o','json'])
capture('nodes-after.json',['kubectl','get','nodes','-o','json'])
gpu=capture('gpu-after.csv',['nvidia-smi','--query-gpu=uuid,name,driver_version,mig.mode.current,utilization.gpu,memory.used','--format=csv'])
processes=capture('compute-after.csv',['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader'])
checks={}
for category in ('operating','queues'):
 before,after=keyed(category+'-before.json'),keyed(category+'-after.json')
 checks[category]={'before_count':len(before),'after_count':len(after),'equal':before==after}
samples=[json.loads(line) for line in (P/'ownership.jsonl').read_text().splitlines()]
checks['ownership']={'samples':len(samples),'foreign_flag':(P/'foreign-owner.json').exists(),'maximum_observation_gap_s':max((b['epoch_ns']-a['epoch_ns'])/1e9 for a,b in zip(samples,samples[1:]))}
checks['gpu_after']=gpu;checks['compute_after']=processes
checks['private_mps_stop']=json.loads((P/'private-mps-stop.json').read_text())
checks['completed_epoch_ns']=time.time_ns()
checks['study_complete']=(P/'study-complete.json').exists()
(P/'postflight.json').write_text(json.dumps(checks,indent=2))
assert all(checks[c]['equal'] for c in ('operating','queues'))
assert checks['private_mps_stop']['returncode']==0
assert not checks['ownership']['foreign_flag']
assert all(line.split(',')[0].strip()=='13025' for line in processes.splitlines())
(P/'stop-watch').touch()
print(json.dumps(checks,indent=2))
