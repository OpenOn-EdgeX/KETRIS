#!/usr/bin/env python3
"""Audit raw seven-worker runtime evidence without changing experiment inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def audit_credit(before, after, events, worker, policy, bank):
    errors, diagnostics = [], []
    fields = ('credit_us', 'charged_us', 'kernels', 'gate_count', 'gate_total_us',
              'refill_accepted', 'refill_rejected', 'granted_us', 'trimmed_us', 'cap_hits')
    delta = {key: after[key] - before[key] for key in fields}
    if before['credit_us'] != 0:
        errors.append('Runtime baseline credit is not zero')
    if before['refill_rejected'] != 0 or after['refill_rejected'] != 0:
        errors.append('Nonzero refill rejection counter')
    if any(delta[key] < 0 for key in fields if key != 'credit_us'):
        errors.append('A cumulative runtime counter decreased')
    residual = delta['credit_us'] - delta['granted_us'] + delta['charged_us'] + delta['trimmed_us']
    if residual:
        errors.append('Credit conservation residual is nonzero')
    if delta['trimmed_us'] > delta['granted_us']:
        errors.append('Trimmed credit exceeds all credit supplied from the zero baseline')
    if delta['charged_us'] <= 0 or delta['kernels'] <= 0:
        errors.append('No charged execution or intercepted kernels')
    if delta['refill_accepted'] != len(events):
        errors.append('Accepted refill count differs from recorded sends')
    if not events:
        errors.append('No finite-credit refill events')
    sent, reduced_ticks, reduced_grants, previous = 0, 0, 0, None
    for event in events:
        at, elapsed = event['at_ns'], event['elapsed_us']
        if not 0 < elapsed <= 50000:
            errors.append('Refill elapsed interval outside runtime policy contract')
        if previous is not None and (at <= previous or elapsed != (at-previous)//1000):
            errors.append('Refill timestamp or elapsed interval mismatch')
        if previous is None and elapsed != 10000:
            errors.append('First refill bootstrap interval differs from 10000 us')
        previous = at
        phases = event['phases']
        if any(value not in ('idle', 'prefill', 'decode', 'done') for value in phases.values()):
            errors.append('Unknown recorded phase')
        phase = phases[worker]
        rate = int(policy.removeprefix('phase'))
        reduced = phase == 'prefill' and any(value == 'decode' for value in phases.values())
        expected = 0 if phase == 'done' else elapsed * (rate if reduced else 100) // 100
        grant = event['grants_us'][worker]
        if grant != expected:
            errors.append('Sent grant violates recorded phase policy')
        if event['caps_us'][worker] != (0 if phase == 'done' else bank):
            errors.append('Sent credit cap differs from configured bank or done cap')
        sent += grant
        if reduced and rate < 100:
            reduced_ticks += 1
            reduced_grants += elapsed-grant
    if sent != delta['granted_us']:
        errors.append('Runtime granted credit differs from sent grant total')
    if events and after['refill_sequence'] != events[-1]['at_ns']:
        errors.append('Last accepted refill sequence differs from final send')
    if events and after['credit_us'] > events[-1]['caps_us'][worker]:
        errors.append('Final credit exceeds the last accepted active/done cap')
    if delta['gate_count'] == 0:
        diagnostics.append('No gate entries observed; finite credit may not have constrained execution')
    if policy != 'phase100' and reduced_ticks == 0:
        diagnostics.append('No sampled prefill/decode overlap triggered a reduced grant')
    return dict(valid=not errors, errors=errors, diagnostics=diagnostics,
                delta=delta, conservation_residual_us=residual, sent_granted_us=sent,
                final_credit_us=after['credit_us'], final_debt_us=max(0, -after['credit_us']),
                gate_count=delta['gate_count'], gate_total_us=delta['gate_total_us'],
                cumulative_gate_count=after['gate_count'], cumulative_gate_total_us=after['gate_total_us'],
                cumulative_gate_max_us=after['gate_max_us'], reduced_refill_ticks=reduced_ticks,
                reduced_grants_relative_to_100pct_us=reduced_grants,
                gate_attribution='Includes startup at zero credit; counts alone do not establish phase-policy engagement')


def audit_steps(requests, start, bank):
    errors, intervals, first_intervals, decode_intervals = [], [], [], []
    previous_done = start
    for index, request in enumerate(requests):
        tokens = request['token_completion_ns']
        began, first, done = (request[key] for key in ('start_ns', 'first_token_ns', 'completion_ns'))
        if len(tokens) != request['output_tokens'] or not tokens:
            errors.append(f'Request {index}: missing or incorrect token timestamp count')
            continue
        if not previous_done <= began <= first <= done or first != tokens[0] or tokens[-1] > done:
            errors.append(f'Request {index}: invalid request/token timestamp ordering')
        steps = [b-a for a, b in zip([began]+tokens[:-1], tokens)]
        if any(value <= 0 for value in steps):
            errors.append(f'Request {index}: nonpositive token interval')
        intervals.extend(steps)
        first_intervals.append(steps[0])
        decode_intervals.extend(steps[1:])
        previous_done = done
    maximum = max(intervals, default=0)/1000
    return dict(valid=not errors, errors=errors,
                maximum_token_interval_us=maximum,
                maximum_start_to_first_token_us=max(first_intervals, default=0)/1000,
                maximum_decode_token_interval_us=max(decode_intervals, default=0)/1000,
                release_to_first_token_us=(requests[0]['first_token_ns']-start)/1000 if requests else None,
                token_interval_count=len(intervals), credit_bank_us=bank,
                exceeds_credit_bank=maximum > bank,
                intervals_exceeding_bank=sum(value > bank*1000 for value in intervals),
                definition='Request start to first synchronized token, then consecutive synchronized token timestamps; includes CPU and gate delays, not an exact runtime charge-window measurement')


def audit_ownership(samples, start, end, identity):
    errors, diagnostics = [], []
    if any(b['epoch_ns'] <= a['epoch_ns'] for a, b in zip(samples, samples[1:])):
        errors.append('Ownership observations are not strictly increasing')
    samples = sorted(samples, key=lambda sample: sample['epoch_ns'])
    before = [i for i, sample in enumerate(samples) if sample['epoch_ns'] <= start]
    after = [i for i, sample in enumerate(samples) if sample['epoch_ns'] >= end]
    if not before:
        errors.append('No ownership observation at or before release')
    if not after:
        errors.append('No ownership observation at or after completion')
    selected = samples[(before[-1] if before else 0):(after[0]+1 if after else len(samples))]
    pod_uid = identity.get('pod_uid')
    if not pod_uid:
        errors.append('Missing ownership watcher pod identity')
    idle, foreign, covered = 0, [], 0
    sample_validity = []
    for sample in selected:
        valid = True
        for process in sample['processes']:
            cgroup = process.get('cgroup', '')
            owned = process.get('owned') is True and pod_uid and (
                pod_uid in cgroup or pod_uid.replace('-', '_') in cgroup)
            idle_allowed = (process.get('allowed_idle_system_mps') is True and
                            str(process['pid']) == str(identity.get('system_idle_mps_pid')) and
                            cgroup.strip().endswith('/system.slice/nvidia-mps.service'))
            if idle_allowed:
                idle += 1
            if not owned and not idle_allowed:
                valid = False
                foreign.append({'epoch_ns': sample['epoch_ns'], 'pid': process.get('pid')})
        sample_validity.append(valid)
    if foreign:
        errors.append('Ownership observation contains an unowned/unallowed GPU process')
    for index, sample in enumerate(selected):
        at = sample['epoch_ns']
        next_at = selected[index+1]['epoch_ns'] if index+1 < len(selected) else end
        if sample_validity[index]:
            covered += max(0, min(end, next_at, at+15_000_000_000)-max(start, at))
    duration = max(0, end-start)
    coverage = 100*covered/duration if duration else 0
    if not duration or covered != duration:
        errors.append('Fresh ownership observation coverage is incomplete at the 15-second guard threshold')
    gaps = [(b['epoch_ns']-a['epoch_ns'])/1e9 for a, b in zip(selected, selected[1:])]
    if gaps and max(gaps) > 3:
        diagnostics.append('Observation interval exceeded the nominal two-second polling interval')
    return dict(valid=not errors, errors=errors, diagnostics=diagnostics,
                samples=len(selected), maximum_observation_gap_s=max(gaps, default=0),
                first_observation_epoch_ns=selected[0]['epoch_ns'] if selected else None,
                last_observation_epoch_ns=selected[-1]['epoch_ns'] if selected else None,
                fresh_observation_coverage_pct=coverage, fresh_observation_covered_seconds=covered/1e9,
                idle_system_mps_observations=idle, foreign_observations=foreign,
                idle_client_list_evidence='Watcher boolean attestation only: each allowed observation means its empty client-list query succeeded; raw query stdout was not retained',
                bracketing_requirement='An observation at or before release and at or after completion is required in addition to full freshness coverage',
                limitation='Periodic observations cannot rule out unobserved activity between polls')


def validate_config(label, cfg, protocol, frozen):
    errors = []
    for key in ('workers', 'gpu_uuid', 'credit_bank_us'):
        if cfg.get(key) != protocol.get(key):
            errors.append('Config differs from protocol: '+key)
    caps = cfg.get('caps', [])
    if len(caps) != 7 or len(set(caps)) != 1 or caps[0] not in (100, 75, 50):
        errors.append('Invalid or nonuniform seven-worker MPS caps')
    if cfg.get('category') == 'holdout':
        matches = [item for item in frozen.get('holdouts', []) if item['label'] == label]
        if len(matches) != 1:
            errors.append('Holdout absent or duplicated in frozen plan')
        else:
            planned = matches[0]
            for key in ('arm', 'repeat', 'policy'):
                if cfg.get(key) != planned[key]:
                    errors.append('Holdout differs from frozen plan: '+key)
            if caps != [planned['cap']]*7:
                errors.append('Holdout caps differ from frozen plan')
        expected_cap = 100 if cfg.get('arm') == 'native' else frozen.get('selected_cap')
        expected_policy = frozen.get('selected_policy') if cfg.get('arm') == 'ketris' else 'native'
        if caps != [expected_cap]*7 or cfg.get('policy') != expected_policy:
            errors.append('Holdout does not use frozen selection for its arm')
    elif cfg.get('category') != 'pilot':
        errors.append('Unknown run category')
    return errors


def validate_cleanup(directory, workers):
    errors = []
    if (directory/'error.json').exists() or (directory/'cleanup-errors.json').exists():
        errors.append('Recorded trial or cleanup error exists')
    if not (directory/'summary.json').is_file():
        errors.append('Successful post-cleanup summary is missing')
    try:
        cleanup = json.loads((directory/'cleanup-complete.json').read_text())
        if cleanup.get('no_live_workers') is not True:
            errors.append('Cleanup did not prove all owned workers exited')
        for worker in workers:
            if cleanup.get('children', {}).get(worker, {}).get('returncode') != 0:
                errors.append('Cleanup has a missing or nonzero worker exit: '+worker)
    except (OSError, ValueError, TypeError) as exc:
        errors.append('Cannot read cleanup receipt: '+str(exc))
    return errors


def phase_overlap(outputs, start, end):
    events, excluded = [], 0
    states = {worker: 'idle' for worker in outputs}
    overlap = {worker: 0 for worker in outputs}
    durations = {worker: {phase: 0 for phase in ('idle', 'prefill', 'decode', 'done')} for worker in outputs}
    for worker, output in outputs.items():
        done = output['completion_ns']
        for event in output['phase_events']:
            if start <= event['at_ns'] <= min(end, done):
                if event['phase'] not in durations[worker]:
                    raise ValueError('Unknown worker phase event')
                events.append((event['at_ns'], worker, event['phase']))
            else:
                excluded += 1
        events.append((min(end, done), worker, 'done'))
    events.sort()
    previous, any_overlap = start, 0
    for at, worker, phase in events+[(end, None, None)]:
        duration = max(0, at-previous)
        decoding = any(value == 'decode' for value in states.values())
        prefilling = any(value == 'prefill' for value in states.values())
        if decoding and prefilling:
            any_overlap += duration
        for name, state in states.items():
            durations[name][state] += duration
            if state == 'prefill' and decoding:
                overlap[name] += duration
        if worker is not None:
            states[worker] = phase
        previous = at
    return dict(any_prefill_decode_overlap_us=any_overlap/1000,
                per_worker_prefill_while_other_decode_us={worker: value/1000 for worker, value in overlap.items()},
                per_worker_phase_duration_us={worker: {phase: value/1000 for phase, value in row.items()}
                                              for worker, row in durations.items()},
                excluded_phase_events=excluded,
                definition='Only phase events inside common release through each worker completion; initial state idle, terminal done synthesized; warmup events excluded')


def worker_lifetime_overlap(outputs):
    latest_start = max(output['started_ns'] for output in outputs.values())
    earliest_finish = min(output['completion_ns'] for output in outputs.values())
    return dict(valid=set(outputs) == {f'W{i}' for i in range(7)} and latest_start < earliest_finish,
                latest_worker_start_ns=latest_start, earliest_worker_completion_ns=earliest_finish,
                overlap_duration_ms=max(0, earliest_finish-latest_start)/1e6,
                definition='Intersection of all seven measured worker lifetimes; does not prove simultaneous GPU kernel execution')


def read_json(path):
    return json.loads(path.read_text())


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_run(directory, context):
    result = dict(label=directory.name, valid=False, errors=[], diagnostics=[], workers={})
    errors = result['errors']
    try:
        cfg, go, summary = (read_json(directory/name) for name in ('config.json', 'go.json', 'summary.json'))
        protocol, frozen = context['protocol'], context['frozen']
        workers = protocol['workers']
        identifiers = [worker['id'] for worker in workers]
        result.update(category=cfg['category'], arm=cfg['arm'], repeat=cfg['repeat'],
                      policy=cfg['policy'], caps=cfg['caps'], start_epoch_ns=go['start_epoch_ns'])
        errors.extend(validate_config(directory.name, cfg, protocol, frozen))
        errors.extend(validate_cleanup(directory, identifiers))
        if summary['config'] != cfg:
            errors.append('Successful summary configuration differs from raw configuration')
        outputs = {worker: read_json(directory/f'result-{worker}.json') for worker in identifiers}
        result['worker_lifetime_overlap'] = worker_lifetime_overlap(outputs)
        if not result['worker_lifetime_overlap']['valid']:
            errors.append('No positive intersection of all seven measured worker lifetimes')
        start = go['start_ns']
        end = max(request['completion_ns'] for output in outputs.values() for request in output['requests'])
        epoch_start, epoch_end = go['start_epoch_ns'], go['start_epoch_ns']+end-start
        result.update(end_epoch_ns=epoch_end, makespan_s=(end-start)/1e9)
        if abs(summary['makespan_s']-result['makespan_s']) > 1e-8:
            errors.append('Summary makespan differs from raw final request completion')
        if cfg['category'] == 'holdout' and frozen['epoch_ns'] >= epoch_start:
            errors.append('Holdout began before configuration was frozen')
        result['ownership'] = audit_ownership(context['ownership'], epoch_start, epoch_end, context['watch_identity'])
        errors.extend('Ownership: '+message for message in result['ownership']['errors'])
        bank = cfg['credit_bank_us']
        before, after, events = (read_json(directory/name) for name in
                                 ('runtime-before.json', 'runtime-after.json', 'feeder-ticks.json'))
        native = cfg['policy'] == 'native'
        if native and (before or after or events):
            errors.append('Native execution unexpectedly has shim credit accounting')
        if not native:
            if set(before) != set(identifiers) or set(after) != set(identifiers):
                errors.append('Runtime snapshots do not cover exactly seven workers')
            for event in events:
                if event['at_ns'] < start:
                    errors.append('Refill recorded before common release')
                if any(set(event[key]) != set(identifiers) for key in ('phases', 'grants_us', 'caps_us')):
                    errors.append('Refill event does not cover exactly seven workers')
        signatures, model_signatures = {}, []
        for work in workers:
            worker = work['id']
            output, cap = outputs[worker], cfg['caps'][int(worker[1:])]
            identity = output['identity']
            worker_errors = []
            if output['id'] != worker or identity['id'] != worker:
                worker_errors.append('Worker identity mismatch')
            if identity['source_sha256'] != context['run_start']['source_hashes']['worker.py']:
                worker_errors.append('Worker source hash differs from execution manifest')
            if identity['uuid'].removeprefix('GPU-') != cfg['gpu_uuid'].removeprefix('GPU-'):
                worker_errors.append('Worker GPU UUID differs from protocol')
            if int(identity['mps_cap']) != cap or identity['reported_sms'] != {100: 188, 75: 140, 50: 94}[cap]:
                worker_errors.append('Worker MPS cap or calibrated SM count mismatch')
            if identity['mps_priority'] != '0' or identity['qmd_callbacks'] != 0 or output['qmd_callbacks_after'] != 0:
                worker_errors.append('Priority or QMD contract violated')
            if identity['dtype'] != 'bfloat16' or identity['parameter_dtypes'] != ['torch.bfloat16']:
                worker_errors.append('Model dtype differs from BF16 contract')
            if identity['batch_size'] != 1 or identity['attention_implementation'] != 'eager':
                worker_errors.append('Batch size or attention implementation differs from protocol')
            if native:
                if identity['mapped_shims'] or identity['libbless_sha256'] is not None:
                    worker_errors.append('Native worker has a runtime shim')
            else:
                if identity['libbless_sha256'] != context['runtime_binary_sha256'] or not identity['mapped_shims']:
                    worker_errors.append('Runtime binary hash or mapped shim evidence mismatch')
                if identity['space_mode'] != 'mps_env':
                    worker_errors.append('Unexpected runtime spatial mode')
                if output['credit_before']['mode'] != 1 or output['credit_after']['mode'] != 1:
                    worker_errors.append('Finite runtime time mode not maintained')
            requests = output['requests']
            if len(requests) != work['requests'] or [request['request_id'] for request in requests] != list(range(work['requests'])):
                worker_errors.append('Request count or sequence differs from fixed work')
            for request in requests:
                if request['input_tokens'] != work['input_tokens'] or request['output_tokens'] != work['output_tokens'] or len(request['token_ids']) != work['output_tokens']:
                    worker_errors.append('Per-request token work differs from protocol')
                output_hash = hashlib.sha256(json.dumps(request['token_ids'], separators=(',', ':')).encode('ascii')).hexdigest()
                if output_hash != request['output_sha256']:
                    worker_errors.append('Stored output hash does not match returned token IDs')
                signatures[f"{worker}/{request['request_id']}"] = [request['input_sha256'], output_hash]
            steps = audit_steps(requests, start, bank)
            worker_errors.extend(steps['errors'])
            if output['completion_ns'] != requests[-1]['completion_ns']:
                worker_errors.append('Worker completion differs from its final request completion')
            row = dict(token_intervals=steps, source_sha256=identity['source_sha256'],
                       runtime_binary_sha256=identity['libbless_sha256'])
            if not native:
                row['credit'] = audit_credit(before[worker], after[worker], events, worker, cfg['policy'], bank)
                row['completion_credit_snapshot'] = output['credit_after']
                row['completion_snapshot_debt_us'] = max(0, -output['credit_after']['credit_us'])
                worker_errors.extend(row['credit']['errors'])
            model_signatures.append({key: identity[key] for key in ('model_config_sha256', 'model_parameters',
                                    'torch', 'transformers', 'dtype', 'attention_implementation', 'batch_size')})
            row.update(valid=not worker_errors, errors=worker_errors)
            result['workers'][worker] = row
            errors.extend(worker+': '+message for message in worker_errors)
        result['work_signatures'] = signatures
        result['model_signatures'] = model_signatures
        result['phase_overlap'] = phase_overlap(outputs, start, end)
        result['maximum_token_interval_us'] = max(row['token_intervals']['maximum_token_interval_us'] for row in result['workers'].values())
        result['token_interval_exceeds_bank'] = result['maximum_token_interval_us'] > bank
        result['credit_bank_us'] = bank
        if result['token_interval_exceeds_bank']:
            result['diagnostics'].append('Observed token interval exceeded the credit bank; performance data retained')
        result['gate_count'] = sum(row.get('credit', {}).get('gate_count', 0) for row in result['workers'].values())
        result['gate_total_us'] = sum(row.get('credit', {}).get('gate_total_us', 0) for row in result['workers'].values())
        result['final_debt_us'] = sum(row.get('credit', {}).get('final_debt_us', 0) for row in result['workers'].values())
        if not native and result['gate_count'] == 0:
            result['diagnostics'].append('No gate entries; performance data retained without claiming temporal policy engagement')
        if native:
            result['diagnostics'].append('Credit-bank comparison is a timing reference only; native arm has no credit bank')
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        errors.append('Cannot audit raw run evidence: '+repr(exc))
    result['valid'] = not errors
    return result


def load_context(root):
    return dict(protocol=read_json(root/'PROTOCOL.json'), frozen=read_json(root/'frozen.json'),
                run_start=read_json(root/'run-start.json'), watch_identity=read_json(root/'watch-identity.json'),
                ownership=[json.loads(line) for line in (root/'ownership.jsonl').read_text().splitlines() if line.strip()],
                runtime_binary_sha256=sha256(root/'libbless.so'))


def validate_frozen_execution(protocol, frozen, runs):
    errors = []
    if frozen.get('workers') != protocol['workers']:
        errors.append('Frozen worker manifest differs from protocol')
    holdouts = sorted((run for run in runs if run.get('category') == 'holdout'),
                      key=lambda run: run.get('start_epoch_ns', -1))
    if [run['label'] for run in holdouts] != [item['label'] for item in frozen['holdouts']]:
        errors.append('Observed holdout chronology differs from frozen execution order')
    return errors


def audit_study(root):
    errors, diagnostics, sources, runs = [], [], {}, []
    result = dict(schema_version=1, root=str(root), valid=False, errors=errors,
                  diagnostics=diagnostics, source_hashes=sources, runs=runs)
    try:
        context = load_context(root)
        protocol, frozen = context['protocol'], context['frozen']
        result['runtime_binary_sha256'] = context['runtime_binary_sha256']
        result['frozen_selection'] = {key: frozen[key] for key in ('epoch_ns', 'selected_cap', 'selected_policy')}
        for name in ('run_study.py', 'worker.py', 'policy.py', 'host_watch.py'):
            actual, expected = sha256(root/name), context['run_start']['source_hashes'].get(name)
            sources[name] = dict(actual=actual, recorded=expected, matches=actual == expected)
            if actual != expected:
                errors.append('Execution source differs from recorded run-start hash: '+name)
        if (root/'foreign-owner.json').exists():
            errors.append('Foreign-owner flag exists')
        directories = sorted(path for path in (root/'runs').iterdir() if path.is_dir())
        runs.extend(audit_run(directory, context) for directory in directories)
        errors.extend(validate_frozen_execution(protocol, frozen, runs))
        if any(not run['valid'] for run in runs):
            errors.append('One or more raw runs failed runtime audit')
        planned = frozen['holdouts']
        expected_pairs = {(arm, repeat) for arm in ('native', 'spatial', 'ketris')
                          for repeat in range(1, protocol['repeats']+1)}
        if len(planned) != len(expected_pairs) or {(item['arm'], item['repeat']) for item in planned} != expected_pairs:
            errors.append('Frozen holdout plan does not contain each required arm/repeat exactly once')
        actual_labels = [run['label'] for run in runs if run.get('category') == 'holdout']
        if sorted(actual_labels) != sorted(item['label'] for item in planned):
            errors.append('Raw holdout directories do not match the complete frozen plan')
        pilots = [run for run in runs if run.get('category') == 'pilot']
        native_pilots = [run for run in pilots if run.get('policy') == 'native']
        if len(native_pilots) != 3 or sorted(run['caps'][0] for run in native_pilots) != [50, 75, 100]:
            errors.append('Missing or duplicated spatial pilot candidate')
        else:
            best = min(native_pilots, key=lambda run: (run['makespan_s'], -run['caps'][0]))
            if best['caps'][0] != frozen['selected_cap']:
                errors.append('Frozen spatial selection differs from raw pilot selection rule')
        credit_pilots = [run for run in pilots if run.get('policy') in ('phase100', 'phase90', 'phase75')]
        if len(credit_pilots) != 3 or {run['policy'] for run in credit_pilots} != {'phase100', 'phase90', 'phase75'}:
            errors.append('Missing or duplicated credit pilot candidate')
        else:
            if any(run['caps'] != [frozen['selected_cap']]*7 for run in credit_pilots):
                errors.append('Credit pilots do not use the selected spatial cap')
            best = min((run for run in credit_pilots if run['policy'] != 'phase100'),
                       key=lambda run: (run['makespan_s'], run['policy'] != 'phase90'))
            if best['policy'] != frozen['selected_policy']:
                errors.append('Frozen temporal selection differs from raw pilot selection rule')
        if pilots and max(run.get('end_epoch_ns', 0) for run in pilots) > frozen['epoch_ns']:
            errors.append('Configuration frozen before pilots completed')
        signatures = [(run['label'], run['work_signatures']) for run in runs if 'work_signatures' in run]
        if signatures:
            reference_label, reference = signatures[0]
            result['input_output_reference_run'] = reference_label
            for label, actual in signatures:
                if actual != reference:
                    errors.append('Input/output hashes differ across fixed-work runs: '+label)
        models = {json.dumps(signature, sort_keys=True) for run in runs for signature in run.get('model_signatures', [])}
        if len(models) != 1:
            errors.append('Model configuration or runtime library versions differ across workers/runs')
        result['holdout_count'] = len(actual_labels)
        result['expected_holdout_count'] = len(expected_pairs)
        result['pilot_count'] = len(pilots)
        result['definitions'] = dict(credit='Per-process host elapsed launch windows; sums are not exclusive GPU time',
            gate_attribution='Includes initial-zero-credit startup; compare phase100 diagnostic pilot before attributing gates to phase reductions',
            bank='Maximum token interval is conservative and includes CPU/gate time; bank exceedance is diagnostic, never an exclusion',
            ownership='Freshness coverage under the existing 15-second watcher guard; this is periodic observation, not an ownership lock',
            final_debt='Final runtime snapshot debt and worker completion-snapshot debt are distinct; completion does not await debt repayment')
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        errors.append('Cannot audit study evidence: '+repr(exc))
    result['valid'] = not errors
    return result


def self_test():
    import copy
    import tempfile
    import unittest

    class RuntimeAuditTests(unittest.TestCase):
        def fixture(self):
            before = dict(credit_us=0, charged_us=100, kernels=10, gate_count=0,
                          gate_total_us=0, gate_max_us=0, refill_accepted=0,
                          refill_rejected=0, granted_us=0, trimmed_us=0, cap_hits=0,
                          refill_sequence=0)
            after = dict(before, credit_us=-500, charged_us=8100, kernels=20,
                         refill_accepted=1, granted_us=7500, refill_sequence=1)
            event = dict(at_ns=1, elapsed_us=10000,
                         phases={'W0': 'prefill', 'W1': 'decode'},
                         grants_us={'W0': 7500, 'W1': 10000},
                         caps_us={'W0': 500000, 'W1': 500000})
            return before, after, [event]

        def test_debt_is_valid_and_no_gate_is_only_diagnostic(self):
            before, after, events = self.fixture()
            result = audit_credit(before, after, events, 'W0', 'phase75', 500000)
            self.assertTrue(result['valid'], result)
            self.assertEqual(result['final_debt_us'], 500)
            self.assertEqual(result['gate_count'], 0)
            self.assertTrue(result['diagnostics'])

        def test_conservation_violation_is_rejected(self):
            before, after, events = self.fixture()
            after['credit_us'] += 1
            result = audit_credit(before, after, events, 'W0', 'phase75', 500000)
            self.assertFalse(result['valid'])
            self.assertTrue(any('conservation' in error for error in result['errors']))

        def test_wrong_phase_grant_is_rejected_even_when_ledger_balances(self):
            before, after, events = self.fixture()
            events[0]['grants_us']['W0'] = 10000
            after.update(granted_us=10000, credit_us=2000)
            result = audit_credit(before, after, events, 'W0', 'phase75', 500000)
            self.assertFalse(result['valid'])
            self.assertTrue(any('policy' in error for error in result['errors']))

        def test_nonzero_refill_rejections_cannot_hide_in_before_snapshot(self):
            before, after, events = self.fixture()
            before['refill_rejected'] = after['refill_rejected'] = 1
            self.assertFalse(audit_credit(before, after, events, 'W0', 'phase75', 500000)['valid'])

        def test_balanced_ledger_cannot_trim_more_credit_than_was_granted(self):
            before, after, events = self.fixture()
            after.update(trimmed_us=10000, credit_us=-10500)
            self.assertFalse(audit_credit(before, after, events, 'W0', 'phase75', 500000)['valid'])

        def test_completed_cap_zero_cannot_leave_positive_final_credit(self):
            before, after, events = self.fixture()
            events.append(dict(at_ns=10000001, elapsed_us=10000,
                phases={'W0': 'done', 'W1': 'decode'},
                grants_us={'W0': 0, 'W1': 10000}, caps_us={'W0': 0, 'W1': 500000}))
            after.update(credit_us=7000, charged_us=600, refill_accepted=2, refill_sequence=10000001)
            self.assertFalse(audit_credit(before, after, events, 'W0', 'phase75', 500000)['valid'])

        def test_first_token_interval_exceeding_bank_does_not_invalidate(self):
            requests = [dict(start_ns=100, first_token_ns=600000100,
                             completion_ns=601000100, output_tokens=2,
                             token_completion_ns=[600000100, 601000100])]
            result = audit_steps(requests, 100, 500000)
            self.assertTrue(result['valid'], result)
            self.assertEqual(result['maximum_token_interval_us'], 600000)
            self.assertTrue(result['exceeds_credit_bank'])

        def ownership_sample(self, at, owned=True):
            return dict(epoch_ns=at, processes=[dict(pid='7', cgroup='podabc',
                         owned=owned, allowed_idle_system_mps=False)])

        def test_ownership_gap_is_not_filled_with_invented_observations(self):
            samples = [self.ownership_sample(0), self.ownership_sample(20000000000)]
            result = audit_ownership(samples, 1000000000, 19000000000,
                                     dict(pod_uid='abc', system_idle_mps_pid='13025'))
            self.assertFalse(result['valid'], result)
            self.assertAlmostEqual(result['fresh_observation_coverage_pct'], 100*14/18)
            self.assertEqual(result['maximum_observation_gap_s'], 20)

        def test_idle_system_allowance_reports_attestation_limit(self):
            rows = [dict(epoch_ns=t, processes=[dict(pid='13025',
                    cgroup='0::/system.slice/nvidia-mps.service', owned=False,
                    allowed_idle_system_mps=True)]) for t in (0, 2000000000)]
            result = audit_ownership(rows, 500000000, 1500000000,
                                     dict(pod_uid='abc', system_idle_mps_pid='13025'))
            self.assertTrue(result['valid'], result)
            self.assertEqual(result['idle_system_mps_observations'], 2)
            self.assertIn('boolean', result['idle_client_list_evidence'])

        def test_holdout_cannot_match_a_constant_but_unselected_policy(self):
            protocol = dict(workers=[dict(id='W0')], gpu_uuid='GPU-test', credit_bank_us=500000)
            frozen = dict(selected_cap=75, selected_policy='phase90', holdouts=[
                dict(label='holdout', arm='ketris', repeat=1, cap=75, policy='phase90')])
            cfg = dict(protocol, category='holdout', arm='ketris', repeat=1,
                       caps=[75]*7, policy='phase75')
            self.assertTrue(validate_config('holdout', cfg, protocol, frozen))

        def test_completed_results_do_not_override_a_recorded_trial_error(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root/'summary.json').write_text('{}')
                (root/'cleanup-complete.json').write_text(json.dumps(dict(no_live_workers=True,
                    children={'W0': dict(returncode=0)})))
                (root/'error.json').write_text('{"error":"late accounting failure"}')
                self.assertTrue(validate_cleanup(root, ['W0']))

        def test_warmup_overlap_is_excluded_and_terminal_done_is_synthesized(self):
            outputs = {
                'W0': dict(completion_ns=3000, phase_events=[
                    dict(at_ns=1, phase='prefill'), dict(at_ns=2000, phase='prefill')]),
                'W1': dict(completion_ns=4000, phase_events=[
                    dict(at_ns=1, phase='decode'), dict(at_ns=1000, phase='decode')]),
            }
            result = phase_overlap(outputs, 1000, 4000)
            self.assertEqual(result['any_prefill_decode_overlap_us'], 1)
            self.assertEqual(result['excluded_phase_events'], 2)

        def test_seven_worker_lifetime_overlap_is_not_sequential_work(self):
            outputs = {f'W{i}': dict(started_ns=i*1000000, completion_ns=20000000)
                       for i in range(7)}
            result = worker_lifetime_overlap(outputs)
            self.assertTrue(result['valid'])
            self.assertEqual(result['overlap_duration_ms'], 14)
            outputs['W0']['completion_ns'] = 5000000
            self.assertFalse(worker_lifetime_overlap(outputs)['valid'])

        def test_changed_frozen_work_and_execution_order_are_rejected(self):
            protocol = {'workers': [{'id': 'W0', 'requests': 8}]}
            frozen = dict(workers=protocol['workers'], holdouts=[{'label': 'a'}, {'label': 'b'}])
            runs = [dict(label='a', category='holdout', start_epoch_ns=20),
                    dict(label='b', category='holdout', start_epoch_ns=10)]
            self.assertTrue(validate_frozen_execution(protocol, frozen, runs))
            runs[0]['start_epoch_ns'] = 5
            frozen['workers'] = [{'id': 'W0', 'requests': 7}]
            self.assertTrue(validate_frozen_execution(protocol, frozen, runs))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeAuditTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return 0 if self_test() else 1
    root = args.root.resolve()
    result = audit_study(root)
    output = root/'runtime-audit.json'
    temporary = output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    temporary.replace(output)
    print(json.dumps(dict(valid=result['valid'], holdouts=result.get('holdout_count', 0),
        pilots=result.get('pilot_count', 0), errors=result['errors'],
        invalid_runs=[dict(label=run['label'], errors=run['errors']) for run in result['runs'] if not run['valid']],
        output=str(output)), indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
