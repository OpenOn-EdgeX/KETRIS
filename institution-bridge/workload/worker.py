"""Continuous direct CUDA GEMM, controlled only by injected KETRIS runtime."""
import ctypes,json,os,time,uuid

def execute(benchmark,identity,expected,emit,clock=time.monotonic,warmup=5,duration=120):
 if identity['device_count']!=1 or identity['actual_cuda_uuid']!=expected:raise ValueError('actual CUDA MIG identity mismatch')
 emit(dict(event='identity',**identity))
 if benchmark.prepare()!=0:raise RuntimeError('CUDA GEMM prepare failed')
 def step():
  code=benchmark.step()
  if code:raise RuntimeError('CUDA GEMM failure '+str(code))
 start=clock()
 while clock()-start<warmup:step()
 emit(dict(event='warmup_complete',**identity));start=clock();report=start;iterations=0
 while clock()-start<duration:
  step();iterations+=1;now=clock()
  if now-report>=1:emit(dict(event='progress',iterations=iterations,elapsed_s=now-start,**identity));report=now
 emit(dict(event='complete',iterations=iterations,elapsed_s=clock()-start,**identity))
def probe(lib):
 cuda=ctypes.CDLL('libcuda.so.1');device=ctypes.c_int();raw=(ctypes.c_ubyte*16)();count=ctypes.c_int()
 for code in (cuda.cuInit(0),cuda.cuDeviceGetCount(ctypes.byref(count)),cuda.cuDeviceGet(ctypes.byref(device),0),cuda.cuDeviceGetUuid_v2(ctypes.byref(raw),device)):
  if code:raise RuntimeError('CUDA identity query failed '+str(code))
 return dict(actual_cuda_uuid='MIG-'+str(uuid.UUID(bytes=bytes(raw))),device_count=count.value,reported_sm_count=lib.sm_count(),mps_space_percent=int(os.environ['CUDA_MPS_ACTIVE_THREAD_PERCENTAGE']),pod_uid=os.environ['POD_UID'],workload_id=os.environ['WORKLOAD_ID'])
def main():
 lib=ctypes.CDLL('/app/gemm.so');execute(lib,probe(lib),os.environ['EXPECTED_MIG'],lambda e:print(json.dumps(e),flush=True))
if __name__=='__main__':main()
