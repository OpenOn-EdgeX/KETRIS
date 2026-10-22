"""Pure experiment contracts; importing never queries or controls hardware."""
import copy
import math
import time


def wait_until(deadline_ns, clock=time.monotonic_ns, sleeper=time.sleep):
    while True:
        remaining = deadline_ns-clock()
        if remaining <= 0:
            return
        sleeper(min(.005, remaining/1e9))


def clean_native_env(env):
    return {k: v for k, v in env.items() if k != 'LD_PRELOAD' and not k.startswith('BLESS_')}


def distribution(values):
    ordered = sorted(values)
    if not ordered or any(not math.isfinite(v) or v < 0 for v in ordered):
        raise ValueError('finite nonnegative observations required')
    # Nearest-rank percentiles: deliberately expose the sample size in the report.
    return {**{f'p{p}': ordered[max(0, math.ceil(len(ordered)*p/100)-1)] for p in (50, 95, 99)},
            'min': ordered[0], 'max': ordered[-1], 'mean': sum(ordered)/len(ordered)}


def summarize_requests(rows, ttft_deadline_ms, completion_deadline_ms, *, arrival_basis='enqueue_ns'):
    if arrival_basis not in ('release_ns','enqueue_ns'):
        raise ValueError('unknown arrival basis')
    metrics = {k: [] for k in ('ttft_ms', 'completion_ms', 'queue_ms', 'enqueue_lag_ms')}
    for r in rows:
        times = [r[k] for k in ('release_ns', 'enqueue_ns', 'start_ns', 'first_token_ns', 'completion_ns')]
        if any(type(t) is not int for t in times) or times != sorted(times) or r['generated_tokens'] < 1:
            raise ValueError('invalid actual request timestamps/token count')
        release, enqueue, start, first, done = times
        arrival=r[arrival_basis]
        for key, elapsed in zip(metrics, (first-arrival, done-arrival, start-enqueue, enqueue-release)):
            metrics[key].append(elapsed/1e6)
    result = {k: distribution(v) for k, v in metrics.items()}
    result.update(requests=len(rows), generated_tokens=sum(r['generated_tokens'] for r in rows),
                  ttft_deadline_ms=ttft_deadline_ms, completion_deadline_ms=completion_deadline_ms,
                  ttft_deadline_misses=sum(v > ttft_deadline_ms for v in metrics['ttft_ms']),
                  completion_deadline_misses=sum(v > completion_deadline_ms for v in metrics['completion_ms']))
    return result


def summarize_batches(rows, window_start_ns, window_end_ns):
    if window_end_ns <= window_start_ns:
        raise ValueError('invalid fixed measurement window')
    good = [r for r in rows if window_start_ns <= r['start_ns'] <= r['end_ns'] <= window_end_ns]
    secs = (window_end_ns-window_start_ns)/1e9
    return dict(window_seconds=secs, completed_batches=len(good),
                completed_prompts=sum(r['prompts'] for r in good),
                input_tokens=sum(r['input_tokens'] for r in good),
                prompts_per_second=sum(r['prompts'] for r in good)/secs,
                boundary_batches_excluded=len(rows)-len(good))


def select_qualified(rows):
    return max((r for r in rows if r['eligible']), key=lambda r:r['background_prompts_s'], default=None)


def cas_node_catalog(current, node, expected, replacement):
    value = copy.deepcopy(current)
    if value.get('nodes', {}).get(node) != expected:
        raise RuntimeError('catalog node changed by another owner; refusing overwrite')
    nodes = value.setdefault('nodes', {})
    if replacement is None:
        nodes.pop(node, None)
    else:
        nodes[node] = replacement
    return value
