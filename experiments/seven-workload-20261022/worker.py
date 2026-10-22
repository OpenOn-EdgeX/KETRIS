"""One fixed-work LLM process for the seven-worker makespan experiment.

Importing this module does not import torch or touch a GPU. The orchestrator owns
MPS setup and shim credit commands, including opening the warmup gate.
"""

import ctypes
import hashlib
import json
import os
import pathlib
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent
MODEL_PATH = pathlib.Path("/shared/model")
PROMPT_TEXT = (
    "This engineering test studies GPU scheduling, inference latency, and fair "
    "resource sharing. Explain the measurements clearly, using practical examples. "
)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, allow_nan=False, separators=(",", ":")))
    temporary.replace(path)


def token_hash(tokens):
    encoded = json.dumps(tokens, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def worker_contract(config, worker_id):
    workers = config.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ValueError("config.workers must be a nonempty list")
    identifiers = [entry.get("id") for entry in workers]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("worker IDs must be unique")
    matches = [entry for entry in workers if entry.get("id") == worker_id]
    if len(matches) != 1:
        raise ValueError("WORKER_ID must select exactly one configured worker")
    selected = matches[0]
    for key in ("input_tokens", "output_tokens", "requests"):
        if type(selected.get(key)) is not int or selected[key] < 1:
            raise ValueError(key + " must be a positive integer")
    return selected


def prompt_tokens(tokenizer, worker_id, request_id, count):
    """Use only tokenizer-produced IDs and preserve an exact context length."""
    prefix = tokenizer.encode(
        f"Worker {worker_id}, request {request_id}. ", add_special_tokens=False
    )
    body = tokenizer.encode(PROMPT_TEXT, add_special_tokens=False)
    if not body:
        raise ValueError("tokenizer returned no prompt tokens")
    repeats = max(0, (count - len(prefix) + len(body) - 1) // len(body))
    result = (prefix + body * repeats)[:count]
    if len(result) != count:
        raise ValueError("prompt construction did not produce the configured length")
    return result


class RunGuard:
    def __init__(self, root, run_dir):
        self.root = root
        self.run_dir = run_dir

    def check(self, allow_run_stop=False):
        if (self.root / "foreign-owner.json").exists():
            raise RuntimeError("foreign GPU owner detected")
        if (self.root / "stop-all").exists():
            raise RuntimeError("experiment stop-all requested")
        if not allow_run_stop and (self.run_dir / "stop").exists():
            raise RuntimeError("trial stop requested")
        heartbeat = self.root / "host-heartbeat.json"
        try:
            age_ns = time.time_ns() - heartbeat.stat().st_mtime_ns
        except FileNotFoundError as exc:
            raise RuntimeError("host ownership heartbeat is missing") from exc
        if age_ns > 15_000_000_000:
            raise RuntimeError("host ownership heartbeat is older than 15 seconds")


def wait_json(path, guard, timeout=900):
    deadline = time.monotonic() + timeout
    while True:
        guard.check()
        if path.exists():
            return json.loads(path.read_text())
        if time.monotonic() >= deadline:
            raise TimeoutError("waiting for " + str(path))
        time.sleep(0.01)


def process_memory():
    result = {}
    for path, label, field in (
        ("/proc/meminfo", "host_available_bytes", "MemAvailable:"),
        ("/proc/self/status", "rss_bytes", "VmRSS:"),
    ):
        for line in pathlib.Path(path).read_text().splitlines():
            if line.startswith(field):
                result[label] = int(line.split()[1]) * 1024
                break
    current = pathlib.Path("/sys/fs/cgroup/memory.current")
    limit = pathlib.Path("/sys/fs/cgroup/memory.max")
    if not current.exists():
        current = pathlib.Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = pathlib.Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if current.exists() and limit.exists():
        result["cgroup_memory_current_bytes"] = int(current.read_text())
        raw_limit = limit.read_text().strip()
        result["cgroup_memory_max_bytes"] = None if raw_limit == "max" else int(raw_limit)
        result["pod_headroom_bytes"] = (
            result.get("host_available_bytes") if raw_limit == "max"
            else int(raw_limit) - result["cgroup_memory_current_bytes"]
        )
    return result


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    run_dir = pathlib.Path(os.environ["RUN_DIR"]).resolve()
    worker_id = os.environ["WORKER_ID"]
    config = json.loads((run_dir / "config.json").read_text())
    work = worker_contract(config, worker_id)
    guard = RunGuard(ROOT, run_dir)
    guard.check()
    phase_events = []

    def publish(phase):
        event = {"phase": phase, "at_ns": time.monotonic_ns()}
        atomic_json(run_dir / ("phase-" + worker_id + ".json"), event)
        phase_events.append(event)

    publish("idle")
    sys.path.insert(0, "/shared/pythonlibs")
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    torch.cuda.set_device(0)
    guard.check()
    library = None
    policy = config.get("policy", "native")
    mapped_shims = sorted({
        line.split()[-1] for line in pathlib.Path("/proc/self/maps").read_text().splitlines()
        if "libbless" in line
    })
    if policy == "native":
        if mapped_shims or "libbless" in os.environ.get("LD_PRELOAD", ""):
            raise RuntimeError("native worker must bypass libbless")
    else:
        library = ctypes.CDLL(str(ROOT / "libbless.so"))
        library.ketris_qmd_launches.restype = ctypes.c_ulonglong
        library.bless_kernel_seq.restype = ctypes.c_longlong
        library.bless_get_total_time_us.restype = ctypes.c_longlong
        library.bless_get_time_credit_us.restype = ctypes.c_longlong
        library.bless_get_credit_mode.restype = ctypes.c_int

    def qmd_callbacks():
        count = int(library.ketris_qmd_launches()) if library is not None else 0
        if count != 0:
            raise RuntimeError("QMD callback path was used: " + str(count))
        return count

    def credit_snapshot():
        if library is None:
            return None
        return {
            "mode": int(library.bless_get_credit_mode()),
            "credit_us": int(library.bless_get_time_credit_us()),
            "charged_us": int(library.bless_get_total_time_us()),
            "kernels": int(library.bless_kernel_seq()),
        }

    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_PATH), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(MODEL_PATH), local_files_only=True,
        dtype=torch.bfloat16, attn_implementation="eager",
    ).eval().to("cuda")
    guard.check()
    prompts = [
        prompt_tokens(tokenizer, worker_id, index, work["input_tokens"])
        for index in range(work["requests"])
    ]
    inputs = [torch.tensor([tokens], dtype=torch.long, device="cuda") for tokens in prompts]
    torch.cuda.synchronize()

    @torch.inference_mode()
    def generate(tokens, count):
        guard.check()
        publish("prefill")
        cache, ids, outputs, first = None, tokens, [], None
        token_completions = []
        for index in range(count):
            guard.check()
            if index == 1:
                publish("decode")
            output = model(
                input_ids=ids, past_key_values=cache,
                use_cache=True, logits_to_keep=1,
            )
            ids = torch.argmax(output.logits[:, -1, :], dim=-1, keepdim=True)
            cache = output.past_key_values
            torch.cuda.synchronize()
            token_completed = time.monotonic_ns()
            token_completions.append(token_completed)
            if first is None:
                first = token_completed
            outputs.append(int(ids.item()))
            qmd_callbacks()
            guard.check()
        completed = time.monotonic_ns()
        publish("idle")
        return first, completed, outputs, token_completions

    for index in range(2):
        generate(inputs[index % len(inputs)], work["output_tokens"])
    props = torch.cuda.get_device_properties(0)
    identity = {
        "id": worker_id,
        "uuid": str(props.uuid),
        "reported_sms": props.multi_processor_count,
        "pid": os.getpid(),
        "mps_cap": os.environ["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"],
        "mps_priority": os.environ["CUDA_MPS_CLIENT_PRIORITY"],
        "policy": policy,
        "space_mode": os.environ.get("BLESS_SPACE_MODE"),
        "dtype": "bfloat16",
        "parameter_dtypes": sorted({str(parameter.dtype) for parameter in model.parameters()}),
        "model": str(MODEL_PATH),
        "model_name_or_path": str(model.config._name_or_path),
        "model_parameters": sum(parameter.numel() for parameter in model.parameters()),
        "model_config_sha256": source_hash(MODEL_PATH / "config.json"),
        "source_sha256": source_hash(pathlib.Path(__file__).resolve()),
        "libbless_sha256": source_hash(ROOT / "libbless.so") if library is not None else None,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "attention_implementation": model.config._attn_implementation,
        "batch_size": 1,
        "warmup_requests": 2,
        "warmup_input_tokens": work["input_tokens"],
        "warmup_output_tokens": work["output_tokens"],
        "mapped_shims": sorted({
            line.split()[-1] for line in pathlib.Path("/proc/self/maps").read_text().splitlines()
            if "libbless" in line
        }),
        "qmd_callbacks": qmd_callbacks(),
        "credit": credit_snapshot(),
        "memory": {
            **process_memory(),
            "cuda_max_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
            "cuda_max_memory_allocated_bytes": torch.cuda.max_memory_allocated(),
            "cuda_memory_reserved_bytes": torch.cuda.memory_reserved(),
            "cuda_memory_allocated_bytes": torch.cuda.memory_allocated(),
        },
    }
    guard.check()
    atomic_json(run_dir / ("ready-" + worker_id + ".json"), identity)
    go = wait_json(run_dir / "go.json", guard)
    start = go["start_ns"]
    if type(start) is not int:
        raise ValueError("go.start_ns must be an integer monotonic timestamp")
    while time.monotonic_ns() < start:
        guard.check()
        time.sleep(min(0.0002, max(0, (start - time.monotonic_ns()) / 1e9)))
    guard.check()
    started = time.monotonic_ns()
    initial_credit = credit_snapshot()
    requests = []
    for index, tokens in enumerate(inputs):
        began = time.monotonic_ns()
        first, completed, generated, token_completions = generate(tokens, work["output_tokens"])
        requests.append({
            "request_id": index,
            "start_ns": began,
            "first_token_ns": first,
            "completion_ns": completed,
            "input_tokens": work["input_tokens"],
            "output_tokens": len(generated),
            "generated_tokens": len(generated),
            "token_ids": generated,
            "token_completion_ns": token_completions,
            "input_sha256": token_hash(prompts[index]),
            "output_sha256": token_hash(generated),
        })
    completed = requests[-1]["completion_ns"]
    guard.check()
    atomic_json(run_dir / ("result-" + worker_id + ".json"), {
        "id": worker_id,
        "requests": requests,
        "identity": identity,
        "started_ns": started,
        "completion_ns": completed,
        "scheduled_start_ns": start,
        "credit_before": initial_credit,
        "credit_after": credit_snapshot(),
        "qmd_callbacks_after": qmd_callbacks(),
        "phase_events": phase_events,
    })
    publish("done")
    print(json.dumps({"id": worker_id, "requests": len(requests), "completion_ns": completed}), flush=True)
    while True:
        guard.check(allow_run_stop=True)
        if (run_dir / "stop").exists():
            return
        time.sleep(0.05)


if __name__ == "__main__":
    main()
