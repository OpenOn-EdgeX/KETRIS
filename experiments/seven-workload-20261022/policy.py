"""Experiment policy: per-client elapsed-time credits, never exclusive GPU time."""
def credit_grants(phases, elapsed_us, policy):
    if policy not in ('phase100','phase90','phase75') or not 0 < elapsed_us <= 50000:
        raise ValueError('invalid policy or refill interval')
    if any(p not in ('prefill','decode','idle','done') for p in phases.values()):
        raise ValueError('invalid phase')
    decoding = any(p == 'decode' for p in phases.values())
    reduced = int(policy[5:])
    return {w: 0 if p == 'done' else elapsed_us*(reduced if p == 'prefill' and decoding else 100)//100
            for w,p in phases.items()}

def whole_makespan(start, completions):
    if set(completions) != {f'W{i}' for i in range(7)} or any(t <= start for t in completions.values()):
        raise ValueError('all seven completed workloads required')
    return max(completions.values())-start
