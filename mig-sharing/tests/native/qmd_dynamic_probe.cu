// Full-GPU hardware regression: QMD must not leave a second affinity ceiling.
// The caller must isolate the device and verify exact hardware/driver first.
#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <dlfcn.h>

#define CK(x) do { cudaError_t e=(x); if(e!=cudaSuccess) { fprintf(stderr,"%s: %s\n",#x,cudaGetErrorString(e)); return 2; } } while(0)
__global__ void record_sm(unsigned* hits, float* sink) {
  unsigned id; asm volatile("mov.u32 %0, %%smid;" : "=r"(id));
  if (threadIdx.x==0) atomicAdd(hits+(id<1024?id:1024),1u);
  float x=threadIdx.x+1.f;
  for (int i=0;i<4096;i++) x=fmaf(x,1.000001f,0.000001f);
  if(threadIdx.x==0) sink[blockIdx.x]=x;
}
int main() {
  cudaDeviceProp p{}; CK(cudaGetDeviceProperties(&p,0));
  if(p.multiProcessorCount!=188 || !strstr(p.name,"RTX PRO 6000")) {
    fprintf(stderr,"Unsupported probe device: %s, %d SM\n",p.name,p.multiProcessorCount); return 3;
  }
  unsigned *hits=nullptr; float* sink=nullptr;
  CK(cudaMalloc(&hits,1025*sizeof(unsigned))); CK(cudaMalloc(&sink,16384*sizeof(float)));
  int fd=socket(AF_UNIX,SOCK_DGRAM,0); sockaddr_un addr{}; addr.sun_family=AF_UNIX;
  snprintf(addr.sun_path,sizeof(addr.sun_path),"/tmp/bless-%d.sock",getpid());
  const int pct[]={100,25,50,100}; const int pct_expected[]={188,48,94,188};
  const unsigned range_begin[]={0,0,32,0,16,0,24,0};
  const unsigned range_count[]={94,32,62,16,78,24,70,94};
  const bool ranges=getenv("KETRIS_PROBE_RANGES")!=nullptr;
  auto set_range=(int(*)(unsigned,unsigned))dlsym(RTLD_DEFAULT,"ketris_qmd_range");
  if(ranges && !set_range)return 5;
  bool okay=true; const bool baseline=getenv("KETRIS_PROBE_BASELINE")!=nullptr;
  for(int k=0;k<(baseline?1:(ranges?8:4));k++) {
    const int expected=ranges?int(range_count[k]*2):pct_expected[k];
    if(ranges){if(set_range(range_begin[k],range_count[k])!=expected)return 6;}
    else if(!baseline){
      char cmd[48]; snprintf(cmd,sizeof(cmd),"qmd_pct %d",pct[k]);
      if(sendto(fd,cmd,strlen(cmd),0,(sockaddr*)&addr,sizeof(addr))<0) { perror("qmd command"); return 4; }
    }
    usleep(30000); // command delivery, not a transition-latency measurement
    unsigned h[1025]{}; CK(cudaMemset(hits,0,sizeof(h)));
    record_sm<<<16384,128>>>(hits,sink); CK(cudaGetLastError()); CK(cudaDeviceSynchronize());
    CK(cudaMemcpy(h,hits,sizeof(h),cudaMemcpyDeviceToHost));
    int observed=0; for(int i=0;i<1024;i++) observed+=h[i]!=0;
    if(ranges) printf("{\"control\":\"range\",\"start_tpc\":%u,\"tpcs\":%u,",range_begin[k],range_count[k]);
    else printf("{\"control\":\"percentage\",\"pct\":%d,",pct[k]);
    printf("\"expected_sms\":%d,\"observed_sms\":%d,\"overflow\":%u,\"sm_ids\":[",expected,observed,h[1024]);
    bool first=true;for(int i=0;i<1024;i++)if(h[i]){printf("%s%d",first?"":",",i);first=false;}printf("]}\n");fflush(stdout);
    okay &= observed==expected && h[1024]==0;
  }
  CK(cudaFree(hits)); CK(cudaFree(sink)); close(fd);
  return okay ? 0 : 1;
}
