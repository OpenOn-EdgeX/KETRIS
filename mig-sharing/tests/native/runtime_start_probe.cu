#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
__global__ void kernel(int* p){*p=42;}
int main(){
  int* p=nullptr;
  if(cudaMalloc(&p,sizeof(int))!=cudaSuccess)return 2;
  int sm=0;cudaDeviceGetAttribute(&sm,cudaDevAttrMultiProcessorCount,0);
  printf("before_launch reported_sm=%d\n",sm);fflush(stdout);
  kernel<<<1,1>>>(p);
  if(cudaGetLastError()!=cudaSuccess||cudaDeviceSynchronize()!=cudaSuccess)return 3;
  printf("launch_complete\n");fflush(stdout);
  // Keep the control socket available for inspection before the harness stops us.
  if(getenv("KETRIS_PROBE_HOLD")) { for(;;){} }
  cudaFree(p);return 0;
}
