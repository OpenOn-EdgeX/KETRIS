"""Stdlib operator: inspect ownership, dry-run and apply only provided objects."""
import argparse,json,subprocess
from dataclasses import dataclass
from pathlib import Path
OWNER='ketris-institution-bridge'
@dataclass(frozen=True)
class DeploymentReport:
 ready:bool;owned_resources:list;image_digests:dict;evidence_dir:str
class Kubectl:
 def run(self,args,body=None):return subprocess.run(['kubectl']+args,input=json.dumps(body) if body else None,text=True,capture_output=True,check=True).stdout
 def existing(self,d):
  args=['get',d['kind'],d['metadata']['name'],'--ignore-not-found','-o','json']
  if d['metadata'].get('namespace'):args+=['-n',d['metadata']['namespace']]
  raw=self.run(args);return json.loads(raw) if raw.strip() else None
 def apply(self,d,dry=False):return self.run(['apply','--server-side','--field-manager=ketris-institution-operator']+(['--dry-run=server'] if dry else [])+['-f','-'],d)
def deploy_owned(manifests,kube,dry=False)->DeploymentReport:
 for d in manifests:
  old=kube.existing(d)
  if old and old.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/managed-by')!=OWNER:raise ValueError('unowned object: '+d['kind']+'/'+d['metadata']['name'])
 for d in manifests:kube.apply(d,dry)
 return DeploymentReport(False,[d['kind']+'/'+d['metadata']['name'] for d in manifests],{},'')
def main():
 p=argparse.ArgumentParser();p.add_argument('manifest',type=Path);p.add_argument('--apply',action='store_true');p.add_argument('--namespaces-only',action='store_true');p.add_argument('--skip-namespaces',action='store_true');a=p.parse_args();docs=json.loads(a.manifest.read_text())
 docs=[d for d in docs if (not a.namespaces_only or d['kind']=='Namespace') and (not a.skip_namespaces or d['kind']!='Namespace')]
 r=deploy_owned(docs,Kubectl(),dry=not a.apply);print(json.dumps(r.__dict__))
if __name__=='__main__':main()
