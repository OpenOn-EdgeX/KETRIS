ARG BASE_IMAGE=ketris-device-plugin:mig-20261013-v9
FROM ${BASE_IMAGE}
COPY backend /opt/ketris/dashboard
RUN python3 -m pip install --no-cache-dir /opt/ketris/dashboard \
 && python3 -c 'import ketris_dashboard.server,ketris_dashboard.observer.service'
COPY frontend /opt/ketris/frontend
COPY archive-bundles /opt/ketris/archive-bundles
COPY build-record.json /opt/ketris/build-record.json
ENTRYPOINT ["ketris-dashboard"]
