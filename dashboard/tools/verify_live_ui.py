#!/usr/bin/env python3
"""Use the real hardware harness; browser/API only observe its execution."""
import argparse,json,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
    run=ROOT/('reports/ketris_mig_runtime_ui_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    with httpx.Client(timeout=3) as client:
        before=client.get(a.url+'/api/v1/nodes/gpu-npu-server-02/snapshot').json()
        (out/'live-before.json').write_text(json.dumps(before,indent=2))
        with (out/'hardware-ui-run.log').open('w') as log:
            process=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/'mig-sharing/tools/verify_live.py'),'--lifecycle-only','--seconds','12','--out',str(run)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            observed=False
            try:
                end=time.monotonic()+240
                while time.monotonic()<end and process.poll() is None:
                    s=client.get(a.url+'/api/v1/nodes/gpu-npu-server-02/snapshot').json()
                    workers=[w for w in s['workloads'] if w['name'] in ['measure-a','measure-b'] and w['applied'].get('reported_sm_count',{}).get('value')==22 and str(w['applied'].get('actual_cuda_uuid',{}).get('value','')).startswith('MIG-')]
                    go=run/'combined50_50_time3_1/go-1'
                    # Two observer cadences after go: the preceding delta window
                    # must be inside measurement, rather than overlap warmup.
                    measuring=go.exists() and time.time()-go.stat().st_mtime>=4
                    if measuring and len(workers)==2 and all(w['observed'].get('kernels',{}).get('value',0)>0 and 0<(w['observed'].get('charged_share',{}).get('value') or 0)<1 for w in workers):
                        assert workers[0]['partition_id']==workers[1]['partition_id']
                        assert sorted(w['applied']['time_share']['value'] for w in workers)==[.25,.75]
                        assert abs(sum(w['observed']['charged_share']['value'] for w in workers)-1)<1e-9
                        (out/'live-mig-snapshot.json').write_text(json.dumps(s,indent=2))
                        subprocess.run(['node',str(ROOT/'dashboard/tools/capture_ui.mjs'),a.url,str(out),'live'],check=True,timeout=30)
                        observed=True;break
                    time.sleep(.5)
                assert observed,'no verified live MIG observation'
            finally:
                # Let the owned hardware process finish its finally restoration.
                try:process.wait(timeout=180)
                except subprocess.TimeoutExpired:process.send_signal(2);process.wait(timeout=180)
            assert process.returncode==0,(out/'hardware-ui-run.log').read_text()[-2500:]
        restored=json.loads((run/'restoration.json').read_text());assert all(r['ok'] for r in restored)
        assert json.loads((run/'restart.json').read_text())['kernel_progress']
        time.sleep(7)
        after=client.get(a.url+'/api/v1/nodes/gpu-npu-server-02/snapshot').json();(out/'live-after.json').write_text(json.dumps(after,indent=2))
        (out/'live-ui-verification.json').write_text(json.dumps({'capture_phase':'measurement','actual_uuid':True,'same_mig_two_workloads':True,'reported_sm':[22,22],'applied_time_shares':[.75,.25],'observed_complete_window_shares':{w['name']:w['observed']['charged_share']['value'] for w in workers},'positive_kernel_counters':True,'browser_live_mig':True,'restoration_verified':True,'hardware_evidence':str(run)},indent=2))
    subprocess.run(['node',str(ROOT/'dashboard/tools/capture_ui.mjs'),a.url,str(out),'replay'],check=True,timeout=60)
    print('Real live MIG and replay UI evidence verified')
if __name__=='__main__':main()
