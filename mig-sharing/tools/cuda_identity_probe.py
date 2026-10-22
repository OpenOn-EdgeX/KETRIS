"""Validate metadata emitted from cuDeviceGetUuid_v2, never from CVD alone."""
import argparse,json
from pathlib import Path
def main():
    p=argparse.ArgumentParser();p.add_argument('metadata');p.add_argument('--mig-uuid',required=True);a=p.parse_args()
    d=json.loads(Path(a.metadata).read_text())
    assert d['actual_cuda_uuid']==a.mig_uuid and d['device_count']==1 and d['cuda_error']==0 and d['sm_error']==0,d
    print(json.dumps(d))
if __name__=='__main__':main()
