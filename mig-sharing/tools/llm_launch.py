"""Exec before torch/CUDA initialization; clean native workers have no preload."""
import os
import sys

env = dict(os.environ)
if env.get('LLM_POLICY') != 'ketris':
    env = {k: v for k, v in env.items() if k != 'LD_PRELOAD' and not k.startswith('BLESS_')}
os.execve(sys.executable, [sys.executable, '-u', '/opt/llm/llm_worker.py'], env)
