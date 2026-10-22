import math
from .contracts import device_uuid
def validate_measurement(row):
    if not row.get('workers') or not row.get('mig_uuid','').startswith('MIG-'): raise ValueError('missing measurement identity')
    device_uuid(row['mig_uuid'])
    for w in row['workers'].values():
        if w.get('actual_cuda_uuid')!=row['mig_uuid']: raise ValueError('actual device differs from requested MIG')
        for key in ['reported_sm_count','iterations','elapsed_s','charged_us','kernels']:
            value=w.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ValueError('missing positive observed '+key)
        if w['kernels']<w['iterations']: raise ValueError('runtime did not account for executed iterations')
