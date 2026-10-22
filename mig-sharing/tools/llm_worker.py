"""Actual pretrained Qwen generation / synchronized batched prefill, eager CUDA."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from llm_experiment import wait_until

OUT = Path(os.environ['LLM_DIR'])
ROLE = os.environ['LLM_ROLE']
POLICY = os.environ['LLM_POLICY']


def save(name, value):
    tmp = OUT / (name + '.tmp-' + ROLE)
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    tmp.replace(OUT / name)


def log(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def wait_file(name):
    deadline = time.monotonic() + 180
    while not (OUT / name).exists():
        if (OUT / 'stop').exists():
            raise RuntimeError('experiment stopped')
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(.01)
    return json.loads((OUT / name).read_text())


def identity():
    lib = ctypes.CDLL('libcuda.so.1')
    device = ctypes.c_int()
    raw = (ctypes.c_ubyte * 16)()
    for rc in (lib.cuInit(0), lib.cuDeviceGet(ctypes.byref(device), 0),
               lib.cuDeviceGetUuid_v2(ctypes.byref(raw), device)):
        if rc != 0:
            raise RuntimeError('CUDA UUID query failed: ' + str(rc))
    maps = Path('/proc/self/maps').read_text()
    mapped = sorted({line.split()[-1] for line in maps.splitlines() if 'libbless' in line})
    result = dict(actual_cuda_uuid='MIG-' + str(uuid.UUID(bytes=bytes(raw))),
                  cuda_device_count=torch.cuda.device_count(),
                  reported_sm_count=torch.cuda.get_device_properties(0).multi_processor_count,
                  space_cap_percent=int(os.environ['CUDA_MPS_ACTIVE_THREAD_PERCENTAGE']),
                  mps_priority=os.environ.get('CUDA_MPS_CLIENT_PRIORITY', '0'),
                  policy=POLICY, mapped_shims=mapped, pid=os.getpid(),
                  versions=dict(torch=torch.__version__, transformers=transformers.__version__, cuda=torch.version.cuda),
                  model_class=type(model).__name__, parameters=sum(p.numel() for p in model.parameters()),
                  dtype=str(next(model.parameters()).dtype), attention='eager', cuda_graphs=False,
                  memory_allocated_bytes=torch.cuda.memory_allocated(), memory_reserved_bytes=torch.cuda.memory_reserved())
    if result['actual_cuda_uuid'] != os.environ['LLM_EXPECTED_MIG'] or result['cuda_device_count'] != 1:
        raise RuntimeError('worker is not isolated to requested MIG')
    if POLICY != 'ketris' and mapped:
        raise RuntimeError('native comparison worker has a loaded shim')
    if POLICY == 'ketris' and not mapped:
        raise RuntimeError('KETRIS worker has no loaded shim')
    return result


import torch
import transformers
from transformers import AutoModelForCausalLM, AutoTokenizer

torch.set_num_threads(2)
torch.cuda.set_device(0)
model_path = os.environ['LLM_MODEL_PATH']
tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True,
            dtype=torch.bfloat16, attn_implementation='eager').eval().to('cuda')

PROMPTS = [
    'Explain why sharing one GPU needs both a resource limit and a scheduling policy.',
    'Give two practical examples of low latency AI services at the network edge.',
    'Explain time credits for GPU inference in clear simple language.',
    'What measurements should an engineer record when comparing inference schedulers?',
]
context = 'This is an engineering test about GPU scheduling, inference latency, and fair background processing. '
a_tokens = []
for prompt in PROMPTS:
    text = tokenizer.apply_chat_template([{'role':'user','content': context*20 + prompt}], tokenize=False, add_generation_prompt=True)
    a_tokens.append(tokenizer(text, return_tensors='pt')['input_ids'][:, -512:].to('cuda'))
bg_length = int(os.environ.get('LLM_BG_TOKENS', '1024'))
bg_batch = int(os.environ.get('LLM_BG_BATCH', '4'))
base = tokenizer(context*400, return_tensors='pt')['input_ids'][:, :bg_length]
bg_tokens = base.repeat(bg_batch, 1).to('cuda')
torch.cuda.synchronize()


@torch.inference_mode()
def generate(tokens, count):
    generated = []
    cache = None
    ids = tokens
    first = None
    for _ in range(count):
        output = model(input_ids=ids, past_key_values=cache, use_cache=True, logits_to_keep=1)
        ids = torch.argmax(output.logits[:, -1, :], dim=-1, keepdim=True)
        cache = output.past_key_values
        torch.cuda.synchronize()
        if first is None:
            first = time.monotonic_ns()
        generated.append(ids.item())
    done = time.monotonic_ns()
    return first, done, generated


@torch.inference_mode()
def batch():
    output = model(input_ids=bg_tokens, use_cache=False, logits_to_keep=1)
    torch.cuda.synchronize()
    # Touch real output only after synchronized completion.
    return float(output.logits[0, -1, 0].item())


if ROLE == 'A':
    generate(a_tokens[0], 2)
else:
    batch()
save('ready-' + ROLE + '.json', identity())
log(dict(event='ready', role=ROLE, **identity()))
mode = wait_file('go.json')
if mode.get('probe'):
    save('probe-before.json', dict(at_ns=time.monotonic_ns()))
    first, done, tokens = generate(a_tokens[0], 2)
    save('probe-after.json', dict(first_token_ns=first, completion_ns=done, tokens=tokens))
else:
    start_ns = mode['start_ns']
    end_ns = mode['end_ns']
    if ROLE == 'A':
        incoming = queue.Queue()
        def producer():
            try:
                for i in range(mode['requests']):
                    release = start_ns + i * mode['interval_ns']
                    wait_until(release)
                    incoming.put((i, release, time.monotonic_ns()))
            except Exception as e:
                incoming.put(e)
        thread = threading.Thread(target=producer)
        thread.start()
        rows = []
        for _ in range(mode['requests']):
            message = incoming.get(timeout=180)
            if isinstance(message, Exception):
                raise message
            i, release, enqueue = message
            began = time.monotonic_ns()
            first, done, generated = generate(a_tokens[i % len(a_tokens)], mode['output_tokens'])
            row = dict(request_id=i, release_ns=release, enqueue_ns=enqueue, start_ns=began,
                       first_token_ns=first, completion_ns=done, generated_tokens=len(generated),
                       input_tokens=a_tokens[i % len(a_tokens)].shape[1], token_ids=generated,
                       text=tokenizer.decode(generated), prompt_id=i % len(PROMPTS))
            rows.append(row)
            with (OUT / 'requests.jsonl').open('a') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            log(dict(event='request_complete', role='A', request_id=i,
                     ttft_ms=(first-enqueue)/1e6, completion_ms=(done-enqueue)/1e6,
                     generated_tokens=len(generated)))
        thread.join()
        save('result-A.json', dict(requests=rows, identity=identity(),
              peak_memory_allocated_bytes=torch.cuda.max_memory_allocated()))
    else:
        while time.monotonic_ns() < start_ns:
            time.sleep(.005)
        rows = []
        # Maintain contention during A's drain; throughput is separately windowed.
        while time.monotonic_ns() < end_ns or not (OUT / 'result-A.json').exists():
            began = time.monotonic_ns()
            checksum = batch()
            row = dict(start_ns=began, end_ns=time.monotonic_ns(), prompts=bg_batch,
                       input_tokens=int(bg_tokens.numel()), output_checksum=checksum)
            rows.append(row)
            with (OUT / 'batches.jsonl').open('a') as f:
                f.write(json.dumps(row) + '\n')
        save('result-B.json', dict(batches=rows, identity=identity(),
              peak_memory_allocated_bytes=torch.cuda.max_memory_allocated()))
        log(dict(event='batch_window_complete', role='B', batches=len(rows)))
# Retain the actual CUDA owner until collection; graceful cleanup is explicit.
while not (OUT / 'stop').exists():
    time.sleep(.1)
