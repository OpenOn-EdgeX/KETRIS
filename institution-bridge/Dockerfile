FROM python:3.12-slim
WORKDIR /app
COPY wheels /wheels
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir --no-index --find-links=/wheels . && rm -rf /wheels
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "ketris_integration.server"]
