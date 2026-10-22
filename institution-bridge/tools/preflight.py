"""Read-only operator preflight; never stops a competing workload."""
from dataclasses import dataclass,asdict
from pathlib import Path
import argparse,json,subprocess,hashlib
@dataclass(frozen=True)
class PreflightReport:
 ok:bool;target:dict;active_gpu_pods:list;shared_fingerprints:dict;reason:str
class Reader:
 def get(self,*args):return json.loads(subprocess.check_output(['kubectl','get',*args,'-o','json'],text=True))
 def partition(self,key):return json.loads(subprocess.check_output(['kubectl','-n','kube-system','exec','ds/ketris-mig-runtime','--','cat','/var/lib/ketris-mig/partitions/'+key.split('/')[-1]+'.json'],text=True))
def preflight(kube,exports=None)->PreflightReport:
 nodes=kube.get('nodes')['items'];target_node=next(n for n in nodes if n['metadata']['name']=='devmaster');keys=[k for k in target_node['status']['allocatable'] if k.startswith('keti.re.kr/mig-')];key=sorted(keys)[0]
 part=kube.partition(key);i=part['inventory'];target={k:i[k] for k in ('gpu_uuid','mig_uuid','resource_name')};target.update(node='devmaster',inventory_generation=i['generation'])
 active=[]
 for p in kube.get('pods','-A')['items']:
  if p.get('status',{}).get('phase') not in ('Pending','Running') or p['spec'].get('nodeName')!='devmaster':continue
  if any(any(k.startswith(('keti.re.kr/mig-','nvidia.com/','keti.re.kr/lsu')) for k in c.get('resources',{}).get('requests',{})) for c in p['spec']['containers']):active.append(p['metadata']['namespace']+'/'+p['metadata']['name'])
 prints={}
 for kind,name,ns in [('deployment','keti-scheduler','keti-exec-system'),('deployment','keti-exec-router','keti-exec-system'),('deployment','keti-queue-api','keti-exec-system'),('daemonset','ketris-device-plugin-lsu','kube-system'),('daemonset','ketris-mig-runtime','kube-system')]:
  spec=kube.get(kind,name,'-n',ns)['spec'];prints[ns+'/'+kind+'/'+name]=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
 for n in ('edge-cq','gpu-cq','npu-cq'):prints['clusterqueue/'+n]=hashlib.sha256(json.dumps(kube.get('clusterqueue',n)['spec'],sort_keys=True).encode()).hexdigest()
 return PreflightReport(not active and part['manager_state']=='running',target,active,prints,'busy: wait; do not stop workloads' if active else 'ready')
def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=preflight(Reader());a.out.mkdir(parents=True,exist_ok=True);(a.out/'preflight.json').write_text(json.dumps(asdict(r),indent=2));(a.out/'target.json').write_text(json.dumps(r.target,indent=2));print(json.dumps(asdict(r)))
if __name__=='__main__':main()
