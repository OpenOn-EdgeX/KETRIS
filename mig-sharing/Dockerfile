# Build context is prepared by tools/build.sh; no report/model/secret files enter it.
ARG BASE_IMAGE=kraken-device-plugin:exp161
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends systemd && rm -rf /var/lib/apt/lists/*
COPY package /opt/ketris/mig-sharing
RUN python3 -m pip install --no-cache-dir --upgrade 'pip==25.2' 'setuptools==80.9.0' \
 && python3 -m pip install --no-cache-dir /opt/ketris/mig-sharing \
 && python3 -c 'import ketris_mig.service, importlib.metadata; assert importlib.metadata.version("ketris-mig") == "0.1.0"'
COPY ketris-mig-plugin /usr/local/bin/ketris-mig-plugin
COPY libbless.so /opt/ketris/libbless.so
COPY runtime_start_probe /opt/ketris/runtime_start_probe
# Compatibility overlay: only the plugin and admission entrypoints use this image.
COPY kraken-device-plugin /opt/kraken/kraken-device-plugin
COPY webhook_server.py /opt/kraken/webhook/webhook_server.py
COPY BUILD_STAMP /opt/kraken/BUILD_STAMP
ENTRYPOINT ["/opt/kraken/entrypoint-plugin.sh"]
