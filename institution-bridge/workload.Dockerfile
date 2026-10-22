FROM ketris-device-plugin:mig-20261013-v9
WORKDIR /app
COPY workload/worker.py workload/gemm.so /app/
ENTRYPOINT ["python3", "/app/worker.py"]
