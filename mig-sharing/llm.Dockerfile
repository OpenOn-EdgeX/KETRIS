FROM prism-infer:opt
COPY tools/llm_launch.py tools/llm_worker.py /opt/llm/
COPY ketris_mig/llm_experiment.py /opt/llm/
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-u", "/opt/llm/llm_launch.py"]
