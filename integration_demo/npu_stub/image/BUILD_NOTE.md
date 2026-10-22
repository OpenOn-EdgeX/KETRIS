# kraken-npu:qwen 빌드 (Exp_83)

이 컴포넌트는 하스퍼/벤더 런타임으로 대체될 수 있습니다. NPU 워크로드 컨테이너화 검증용입니다.

## 구성
- 이미지: ubuntu:22.04 + python3.10 + libgomp1/libnuma1 + 호스트 furiosa 런타임 libs(libfuriosa_*.so).
- furiosa SDK venv·모델·워커는 **hostPath 마운트**(이미지엔 런타임 libs만, 비대화 방지).

## 빌드
```bash
# 빌드 컨텍스트에 호스트 furiosa libs 복사
mkdir -p ctx/libfuriosa && cp -P /usr/lib/x86_64-linux-gnu/libfuriosa_*.so* ctx/libfuriosa/
cp Dockerfile ctx/ && cd ctx
docker build -t kraken-npu:qwen .
docker save kraken-npu:qwen -o kraken-npu-qwen.tar
ctr -n k8s.io images import kraken-npu-qwen.tar   # kubelet 사용 네임스페이스
```

## 마운트 (파드, npu_pod.tmpl.yaml 참조)
- venv site-packages → /venv/site-packages (PYTHONPATH), venv bin → /venv/bin
- HF 캐시(모델) → /hf, furiosa 캐시 → /furiosa-cache, worker → /work
- PE 블록: plugin이 DeviceSpec(/dev/rngd/npu1pe0-3 등) + env(KETI_NPU_DEVICES) 주입
