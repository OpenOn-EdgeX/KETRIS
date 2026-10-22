FROM ubuntu:22.04
# NPU 런타임 이미지 (Exp_83). furiosa venv/model/worker는 hostPath 마운트 — 이미지엔 python+런타임 libs만.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.10 libgomp1 libnuma1 libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/local/bin/python
COPY libfuriosa/ /usr/lib/x86_64-linux-gnu/
RUN ldconfig
ENV PYTHONUNBUFFERED=1
