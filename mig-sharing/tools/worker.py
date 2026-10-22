import ctypes,json,os,time
from pathlib import Path
d=Path(os.environ['MEASURE_DIR']);name=os.environ['MEASURE_NAME'];lib=ctypes.CDLL('/lab/gemm.so')
duration=float(os.environ.get('MEASURE_SECONDS','20'));repeats=int(os.environ.get('MEASURE_REPEATS','3'))
assert lib.prepare()==0
def step():
    result=lib.step()
    if result:raise RuntimeError('CUDA error '+str(result))
start=time.monotonic()
while time.monotonic()-start<5:step()
(d/('ready-'+name)).write_text('ready')
for repeat in range(1,repeats+1):
    while not (d/f'go-{repeat}').exists():time.sleep(.01)
    start=time.monotonic();n=0
    while time.monotonic()-start<duration:step();n+=1
    (d/f'result-{name}-{repeat}.json').write_text(json.dumps({'iterations':n,'elapsed_s':time.monotonic()-start,'reported_sm_count':lib.sm_count()}))
while not (d/'stop').exists():
    if (d/'resume').exists():step()
    else:time.sleep(.02)
