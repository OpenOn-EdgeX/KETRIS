"""Stage only explicit bridge sources; never copies a workspace or credentials."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def stage_sources(out:Path)->dict:
 out.mkdir(parents=True,exist_ok=True)
 for name in ('src','proto','workload'):shutil.copytree(ROOT/name,out/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
 for name in ('pyproject.toml','Dockerfile','workload.Dockerfile'):shutil.copy2(ROOT/name,out/name)
 source=ROOT.parent/'mig-sharing/tests/native/gemm.cu';shutil.copy2(source,out/'workload/gemm.cu')
 hashes={str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.rglob('*')) if p.is_file() and p.name!='manifest.json'}
 result=dict(source_sha256=hashes,bridge_tag='ketris-institution-bridge:20261015-v1',worker_tag='ketris-institution-gemm:20261015-v1');(out/'manifest.json').write_text(json.dumps(result,indent=2));return result
def main():
 p=argparse.ArgumentParser();p.add_argument('--stage',type=Path,default=ROOT.parent/'.build/institution-bridge');p.add_argument('--compile',action='store_true');a=p.parse_args();r=stage_sources(a.stage)
 if a.compile:subprocess.run(['/usr/local/cuda-12.9/bin/nvcc','-O3','-shared','-Xcompiler','-fPIC','--cudart','shared','-arch=sm_120',str(a.stage/'workload/gemm.cu'),'-o',str(a.stage/'workload/gemm.so')],check=True)
 print(json.dumps(r,indent=2))
if __name__=='__main__':main()
