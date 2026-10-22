"""Portable stdlib viewer of recorded evidence, not a new device execution."""
import argparse,hashlib,json
from pathlib import Path

def render(root:Path,section='all'):
 verification=json.loads((root/'verification.json').read_text())
 if not verification['passed']:raise ValueError('run verification did not pass')
 manifest={x['path']:x for x in json.loads((root/'evidence-manifest.json').read_text())}
 def read(name):
  raw=(root/name).read_bytes()
  if name not in manifest or hashlib.sha256(raw).hexdigest()!=manifest[name]['sha256']:raise ValueError('source SHA256 mismatch: '+name)
  return json.loads(raw)
 info=read('run-info.json');a=read('state-a-final.json');b=read('state-b-final.json');target=read('target.json');lines=['KETRIS | recorded evidence summary | '+info['run_id']]
 if section in ('rpc','all'):
  lines += ['[INPUT] Yonsei: SYNTHETIC GEMM2048 fp32; GPU memory=1GiB (assumption)','[INPUT] SKKU: '+target['mig_uuid'],'[PLAN] node='+target['node']+' cap A/B=50/50%; time weight=3:1']
 if section in ('guard','all'):
  g=read('guard-rejected.json');ac={x['name']:x for x in g['workload']['status']['admissionChecks']}
  lines += ['[GUARD] operator Workload weight drift: KETI='+ac['keti-scheduler']['state']+' / plan guard='+ac['ketris-plan-validation']['state'],'[GUARD] suspended='+str(g['job']['spec']['suspend'])+'; GPU Pods='+str(len(g['pods']))]
 if section in ('gpu','all'):
  for label,s in [('A',a),('B',b)]:
   e=json.loads(s['applied']['evidence_json']);lines += [f'[{label}] {s["state"]} | CUDA SM={e["reported_sm_count"]} | cap={e["space_percent"]}%',f'[{label}] last_ACK_budget={e["budget_us"]}us/10ms; Job UID={s["job_uid"]}']
  for index,m in enumerate(verification['measurements']):lines += [f'[WINDOW {index+1}] {m["elapsed_s"]:.3f}s | A={m["a_share_percent"]:.4f}% | error={m["error_pp"]:.4f}pp']
  lines += ['[CHAIN] existing KETI + own guard Ready -> Kueue -> default-scheduler -> KETRIS','[LIMIT] charged kernel wall time; MPS cap is not exclusive SM ownership']
 return '\n'.join(lines)+'\n'
def main():
 p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('--section',choices=['all','rpc','guard','gpu'],default='all');p.add_argument('--raw',action='store_true');a=p.parse_args()
 if a.raw:
  for f in sorted(a.root.glob('worker-*.log')):print(f.name+'\n'+f.read_text())
 else:print(render(a.root,a.section),end='')
if __name__=='__main__':main()
