// workload_cuda_kernel.cu
// cuLaunchKernel을 PLT(ELF 링크)를 통해 직접 호출하는 GEMM 워크로드.
// gating_lib.so의 cuLaunchKernel hook 검증에 사용.
//
// 빌드:
//   nvcc -O2 --compiler-options="-fPIC" -shared \
//     -o workload_cuda_kernel.so workload_cuda_kernel.cu \
//     -lcuda -lcudart
//
// 실행 (Python ctypes로):
//   tests/workload_cuda.py

#include <cuda.h>
#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>

// ── 단순 행렬 곱 커널 (cuBLAS 미사용) ────────────────────────────────────────
// 이 커널은 cuLaunchKernel (Driver API)를 통해 직접 제출됨 → hook 동작
__global__
void matmul_kernel(const float* A, const float* B, float* C, int N) {
    int row = blockIdx.y * blockDim.y + threadIdx.y;
    int col = blockIdx.x * blockDim.x + threadIdx.x;
    if (row >= N || col >= N) return;

    float sum = 0.0f;
    for (int k = 0; k < N; k++)
        sum += A[row * N + k] * B[k * N + col];
    C[row * N + col] = sum;
}

// ── Python ctypes에서 호출하는 C 인터페이스 ───────────────────────────────────
extern "C" {

// 초기화: GPU 메모리 할당 + 데이터 초기화
// 반환: 0 = 성공, 비0 = 실패
int workload_init(int N, void** pA, void** pB, void** pC) {
    size_t bytes = (size_t)N * N * sizeof(float);

    cudaError_t r = cudaMalloc(pA, bytes);
    if (r != cudaSuccess) return -1;
    r = cudaMalloc(pB, bytes);
    if (r != cudaSuccess) { cudaFree(*pA); return -2; }
    r = cudaMalloc(pC, bytes);
    if (r != cudaSuccess) { cudaFree(*pA); cudaFree(*pB); return -3; }

    // 1로 초기화
    cudaMemset(*pA, 1, bytes);
    cudaMemset(*pB, 1, bytes);
    cudaMemset(*pC, 0, bytes);
    cudaDeviceSynchronize();
    return 0;
}

// 행렬 곱 실행 (1회)
void workload_run(void* A, void* B, void* C, int N) {
    dim3 block(16, 16);
    dim3 grid((N + 15) / 16, (N + 15) / 16);
    matmul_kernel<<<grid, block>>>(
        (const float*)A, (const float*)B, (float*)C, N);
}

// 정리
void workload_free(void* A, void* B, void* C) {
    cudaFree(A);
    cudaFree(B);
    cudaFree(C);
}

} // extern "C"
