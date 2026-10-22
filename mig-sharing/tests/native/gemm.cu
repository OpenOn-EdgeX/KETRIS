#include <cuda_runtime.h>
#include <initializer_list>
static float *a, *b, *c;
constexpr int N=2048, T=16;
__global__ void gemm_verify(const float* a,const float* b,float* c){
    __shared__ float aa[T][T],bb[T][T];
    int row=blockIdx.y*T+threadIdx.y,col=blockIdx.x*T+threadIdx.x;
    float v=0;
    for(int k=0;k<N/T;k++){
        aa[threadIdx.y][threadIdx.x]=a[row*N+k*T+threadIdx.x];
        bb[threadIdx.y][threadIdx.x]=b[(k*T+threadIdx.y)*N+col];
        __syncthreads();
        #pragma unroll
        for(int j=0;j<T;j++)v=fmaf(aa[threadIdx.y][j],bb[j][threadIdx.x],v);
        __syncthreads();
    }
    c[row*N+col]=v;
}
extern "C" int prepare(){
    cudaError_t e;
    for(float** p:{&a,&b,&c}){
        e=cudaMalloc((void**)p,N*N*sizeof(float));if(e!=cudaSuccess)return int(e);
        e=cudaMemset(*p,0,N*N*sizeof(float));if(e!=cudaSuccess)return int(e);
    }
    return int(cudaDeviceSynchronize());
}
extern "C" int step(){
    gemm_verify<<<dim3(N/T,N/T),dim3(T,T)>>>(a,b,c);
    cudaError_t e=cudaGetLastError();if(e!=cudaSuccess)return int(e);
    return int(cudaDeviceSynchronize());
}
extern "C" int sm_count(){int n=0;cudaDeviceGetAttribute(&n,cudaDevAttrMultiProcessorCount,0);return n;}
