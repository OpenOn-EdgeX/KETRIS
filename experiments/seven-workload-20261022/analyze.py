#!/usr/bin/env python3
"""Audit fixed-work, seven-worker makespan trials; use --self-test for fixtures."""
import argparse
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from datetime import datetime, timezone


ARMS = ('native', 'spatial', 'ketris')
WORKER_IDS = {f'W{i}' for i in range(7)}


def read_json(path):
    with Path(path).open() as f:
        return json.load(f)


def canonical_workers(workers):
    if not isinstance(workers, list):
        raise ValueError('workers must be a list')
    if len(workers) != 7 or {w.get('id') for w in workers} != WORKER_IDS:
        raise ValueError('manifest must contain exactly W0 through W6')
    return sorted(workers, key=lambda w: w['id'])


def stats(values):
    if not values:
        return dict(n=0, mean=None, std=None, min=None, max=None)
    return dict(n=len(values), mean=statistics.mean(values),
                std=statistics.stdev(values) if len(values) > 1 else 0.0,
                min=min(values), max=max(values))


def timestamp_seconds(value, column):
    value = value.strip()
    try:
        number = float(value)
        if 'ns' in column or abs(number) > 1e17:
            return number / 1e9
        if 'ms' in column or abs(number) > 1e11:
            return number / 1e3
        return number
    except ValueError:
        for fmt in ('%Y/%m/%d %H:%M:%S.%f', '%Y/%m/%d %H:%M:%S',
                    '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def device_busy(path, start_epoch_ns, end_epoch_ns, max_interval_s=0.6):
    result = dict(available=False, source=str(path) if path else None,
                  coverage_pct=0.0, covered_seconds=0.0,
                  utilization_pct=None, forward_utilization_pct=None,
                  backward_utilization_pct=None, errors=[], warnings=[],
                  method='Trapezoid of adjacent samples over covered time only; no zero imputation',
                  maximum_accepted_interval_s=max_interval_s)
    if path is None:
        result['warnings'].append('No NVML CSV found')
        return result
    samples = []
    try:
        with Path(path).open(newline='') as f:
            rows = list(csv.reader(f))
            if not rows:
                raise ValueError('Empty NVML CSV')
            try:
                timestamp_seconds(rows[0][0], 'timestamp')
                float(rows[0][1].strip().rstrip('%').strip())
                headerless = True
            except (ValueError, IndexError):
                headerless = False
            if headerless:
                # The orchestrator's explicit nvidia-smi query order.
                fields = ['timestamp', 'utilization.gpu', 'utilization.memory',
                          'power.draw', 'temperature.gpu', 'clocks.current.sm',
                          'clocks.current.memory', 'hw_thermal_slowdown',
                          'sw_thermal_slowdown', 'sw_power_cap', 'hw_power_brake_slowdown']
                data_rows, first_line = rows, 1
            else:
                fields, data_rows, first_line = rows[0], rows[1:], 2
            reader = (dict(zip(fields, row)) for row in data_rows)
            names = {name.strip().lower(): name for name in fields}
            result['headerless_query_layout'] = headerless
            tcol = next((names[k] for k in names if k in ('timestamp', 'timestamp_ns',
                        'timestamp_epoch_ns', 'epoch_ns', 'epoch_s', 'time', 'timestamp_ms')), None)
            ucol = next((names[k] for k in names if k.startswith('utilization.gpu') or
                         k in ('gpu_util', 'gpu_utilization', 'util.gpu')), None)
            if not tcol or not ucol:
                raise ValueError('CSV requires timestamp and utilization.gpu columns')
            for line, row in enumerate(reader, first_line):
                try:
                    t = timestamp_seconds(row[tcol], tcol.lower())
                    u = float(row[ucol].strip().rstrip('%').strip())
                    if not math.isfinite(t) or not math.isfinite(u) or not 0 <= u <= 100:
                        raise ValueError('nonfinite or out-of-range sample')
                    samples.append((t, u))
                except (TypeError, ValueError, KeyError) as exc:
                    result['warnings'].append(f'CSV line {line}: {exc}')
    except (OSError, ValueError) as exc:
        result['errors'].append(str(exc))
        return result
    result['sample_count'] = len(samples)
    if len(samples) < 2:
        result['errors'].append('Fewer than two usable samples')
        return result
    gaps = [b[0]-a[0] for a, b in zip(samples, samples[1:])]
    if any(gap <= 0 for gap in gaps):
        result['errors'].append('NVML sample timestamps are not strictly increasing')
        return result
    start, end = start_epoch_ns / 1e9, end_epoch_ns / 1e9
    result.update(capture_started_before_work=samples[0][0] <= start,
                  capture_ended_after_work=samples[-1][0] >= end,
                  max_gap_s=max(gaps), median_gap_s=statistics.median(gaps))
    if not result['capture_started_before_work']:
        result['warnings'].append('Capture starts after workload start')
    if not result['capture_ended_after_work']:
        result['warnings'].append('Capture ends before final workload completion')
    covered = forward = backward = 0.0
    for (ta, ua), (tb, ub) in zip(samples, samples[1:]):
        lo, hi = max(ta, start), min(tb, end)
        if hi <= lo or tb-ta > max_interval_s:
            continue
        duration = hi-lo
        covered += duration
        forward += duration*ua
        backward += duration*ub
    result['covered_seconds'] = covered
    result['coverage_pct'] = 100*covered/(end-start) if end > start else 0.0
    if covered:
        fwd, bwd = forward/covered, backward/covered
        result.update(available=True, utilization_pct=(fwd+bwd)/2,
                      forward_utilization_pct=fwd, backward_utilization_pct=bwd,
                      sensitivity_absolute_pct=abs(fwd-bwd),
                      device_busy_seconds_covered=(forward+backward)/200)
    if result['coverage_pct'] < 99:
        result['warnings'].append('Incomplete temporal coverage; utilization applies only to covered time')
    return result


def audit_run(directory, frozen_workers, root):
    r = dict(label=directory.name, path=str(directory), valid=False, errors=[],
             warnings=[], workers=[], token_signatures={}, input_signatures={}, makespan_s=None,
             requests_per_s=None, output_tokens_per_s=None, workloadsets_per_s=None)
    if (directory/'error.json').exists():
        r['errors'].append('Orchestrator error.json exists; run cannot count as successful')
    try:
        cfg = read_json(directory/'config.json')
        r.update(category=cfg.get('category'), arm=cfg.get('arm'), repeat=cfg.get('repeat'),
                 policy=cfg.get('policy'), caps=cfg.get('caps'))
        if r['category'] not in ('pilot', 'holdout'):
            r['errors'].append('Unknown or missing category')
        if r['arm'] not in ARMS:
            r['errors'].append('Unknown or missing arm')
        if not isinstance(r['repeat'], int):
            r['errors'].append('Repeat must be an integer')
        workers = canonical_workers(cfg['workers'])
        if frozen_workers is None or workers != frozen_workers:
            r['errors'].append('Worker config differs from frozen manifest')
        go = read_json(directory/'go.json')
        start, epoch = int(go['start_ns']), int(go['start_epoch_ns'])
        r.update(start_ns=start, start_epoch_ns=epoch)
        completions, total_requests, total_output = [], 0, 0
        identities = []
        for w in workers:
            wid = w['id']
            wr = dict(id=wid, expected_requests=w['requests'], actual_requests=0,
                      expected_output_tokens_per_request=w['output_tokens'], errors=[])
            r['workers'].append(wr)
            try:
                out = read_json(directory/f'result-{wid}.json')
                if out.get('error'):
                    wr['errors'].append(f"Worker reported error: {out['error']}")
                if out.get('id') != wid:
                    wr['errors'].append('Worker ID mismatch')
                reqs = out['requests']
                wr['actual_requests'] = len(reqs)
                total_requests += len(reqs)
                if len(reqs) != w['requests']:
                    wr['errors'].append('Request count differs from manifest')
                identity = out.get('identity', {})
                wr['identity'] = identity
                identities.append(identity)
                if not identity.get('uuid') or not identity.get('reported_sms'):
                    wr['errors'].append('Missing GPU UUID or reported SM count')
                if not isinstance(identity.get('pid'), int):
                    wr['errors'].append('Missing worker PID')
                for field in ('source_sha256', 'model_config_sha256'):
                    if not isinstance(identity.get(field), str) or len(identity[field]) != 64:
                        wr['errors'].append(f'Missing or invalid identity {field}')
                library_hash = identity.get('libbless_sha256')
                if cfg.get('policy') != 'native':
                    if not isinstance(library_hash, str) or len(library_hash) != 64:
                        wr['errors'].append('Missing or invalid identity libbless_sha256')
                elif library_hash is not None:
                    wr['errors'].append('Native policy unexpectedly loaded libbless')
                expected_cap = (cfg.get('caps') or [None]*7)[int(wid[1:])]
                if expected_cap is not None and int(identity.get('mps_cap', -1)) != int(expected_cap):
                    wr['errors'].append('Worker MPS cap differs from config')
                previous = start
                seen = set()
                actual_output = 0
                for req in reqs:
                    rid = str(req['request_id'])
                    if rid in seen:
                        wr['errors'].append(f'Duplicate request ID {rid}')
                    seen.add(rid)
                    rs, ft, done = (int(req[k]) for k in
                                   ('start_ns', 'first_token_ns', 'completion_ns'))
                    if not previous <= rs <= ft <= done:
                        wr['errors'].append(f'Invalid request timestamp ordering: {rid}')
                    previous = done
                    completions.append(done)
                    tokens = req.get('token_ids')
                    if not isinstance(tokens, list) or not all(isinstance(t, int) for t in tokens):
                        wr['errors'].append(f'Missing integer token IDs: {rid}')
                        tokens = []
                    if req.get('input_tokens') != w['input_tokens']:
                        wr['errors'].append(f'Input token count mismatch: {rid}')
                    input_hash = req.get('input_sha256')
                    if not isinstance(input_hash, str) or len(input_hash) != 64:
                        wr['errors'].append(f'Missing or invalid input_sha256: {rid}')
                    r['input_signatures'][f'{wid}/{rid}'] = input_hash
                    if req.get('output_tokens') != w['output_tokens'] or len(tokens) != w['output_tokens']:
                        wr['errors'].append(f'Output token count mismatch: {rid}')
                    actual_output += int(req.get('output_tokens', 0))
                    r['token_signatures'][f'{wid}/{rid}'] = hashlib.sha256(
                        json.dumps(tokens, separators=(',', ':')).encode()).hexdigest()
                wr['actual_output_tokens'] = actual_output
                total_output += actual_output
                completion = int(out['completion_ns'])
                started = int(out['started_ns'])
                if started < start or (reqs and started > int(reqs[0]['start_ns'])):
                    wr['errors'].append('Worker started timestamp is outside request interval')
                if reqs and completion < max(int(q['completion_ns']) for q in reqs):
                    wr['errors'].append('Worker completion precedes request completion')
                wr['completion_ns'] = completion
            except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                wr['errors'].append(f'Cannot audit worker: {exc}')
            r['errors'].extend(f'{wid}: {e}' for e in wr['errors'])
        if len({i.get('uuid') for i in identities if i.get('uuid')}) > 1:
            r['errors'].append('Workers used different GPU UUIDs')
        for field in ('source_sha256', 'model_config_sha256', 'libbless_sha256'):
            values = {i.get(field) for i in identities if i.get(field) is not None}
            if len(values) > 1:
                r['errors'].append(f'Worker identity {field} changed within run')
        pids = [i.get('pid') for i in identities if i.get('pid') is not None]
        if len(set(pids)) != len(pids):
            r['errors'].append('Worker PIDs are not unique')
        if not (directory/'cleanup-complete.json').is_file():
            r['errors'].append('Missing cleanup-complete.json')
        else:
            cleanup = read_json(directory/'cleanup-complete.json')
            r['cleanup'] = cleanup
            if cleanup.get('ok') is False or cleanup.get('error'):
                r['errors'].append('Cleanup reported failure')
            if cleanup.get('no_live_workers') is not True:
                r['errors'].append('Cleanup does not confirm no_live_workers=true')
            children = cleanup.get('children')
            if not isinstance(children, dict):
                r['errors'].append('Cleanup has no child process exit records')
                children = {}
            for wid in sorted(WORKER_IDS):
                child = children.get(wid)
                if not isinstance(child, dict) or child.get('returncode') != 0:
                    r['errors'].append(f'Cleanup: {wid} missing or did not exit with returncode 0')
        if completions:
            last = max(completions)
            seconds = (last-start)/1e9
            end_epoch = epoch + last-start
            r.update(last_request_completion_ns=last, end_epoch_ns=end_epoch,
                     makespan_s=seconds, actual_requests=total_requests,
                     actual_output_tokens=total_output)
            if seconds <= 0:
                r['errors'].append('Nonpositive makespan')
            else:
                r.update(requests_per_s=total_requests/seconds,
                         output_tokens_per_s=total_output/seconds,
                         workloadsets_per_s=1/seconds)
            nvml = next((p for p in (directory/'nvml.csv', directory/'gpu.csv', root/'nvml.csv') if p.is_file()), None)
            r['device_busy'] = device_busy(nvml, epoch, end_epoch)
            if not (directory/'summary.json').is_file():
                r['errors'].append('Missing orchestrator summary.json')
            else:
                summary = read_json(directory/'summary.json')
                r['orchestrator_summary'] = summary
                if not isinstance(summary, dict) or summary.get('error') or summary.get('ok') is False:
                    r['errors'].append('Orchestrator summary does not report success')
                else:
                    if summary.get('label') != directory.name:
                        r['errors'].append('Orchestrator summary label differs from run directory')
                    if 'config' in summary and summary['config'] != cfg:
                        r['errors'].append('Orchestrator summary config differs from config.json')
                    expected = {'makespan_s': seconds, 'completed_requests': total_requests,
                                'generated_tokens': total_output, 'end_epoch_ns': end_epoch}
                    for field, value in expected.items():
                        reported = summary.get(field)
                        matches = (isinstance(reported, (int, float)) and
                                   math.isclose(reported, value, rel_tol=1e-10, abs_tol=1e-9)) if field == 'makespan_s' else reported == value
                        if not matches:
                            r['errors'].append(f'Orchestrator summary {field} differs from raw results')
        else:
            r['errors'].append('No request completions available')
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        r['errors'].append(f'Cannot audit run: {exc}')
    r['valid'] = not r['errors']
    return r


def validate_frozen_selection(root, runs, frozen_workers, expected_repeats):
    """Match every held-out trial to the selection and schedule saved before execution."""
    holdouts = [r for r in runs if r.get('category') == 'holdout']
    path = root/'frozen.json'
    if not path.is_file():
        return None, ['Missing frozen.json holdout selection'] if holdouts else []
    errors = []
    try:
        selected = read_json(path)
        if canonical_workers(selected['workers']) != frozen_workers:
            errors.append('frozen.json workers differ from frozen-manifest.json')
        cap = selected['selected_cap']
        policy = selected['selected_policy']
        if not isinstance(cap, int) or not 0 < cap <= 100:
            errors.append('Invalid selected_cap in frozen.json')
        if policy not in ('phase90', 'phase75'):
            errors.append('Invalid selected_policy in frozen.json')
        plan = selected['holdouts']
        if not isinstance(plan, list) or len(plan) != expected_repeats*len(ARMS):
            errors.append('Frozen holdout plan does not contain expected number of trials')
        by_label = {}
        for item in plan:
            label = item['label']
            if label in by_label:
                errors.append(f'Duplicate frozen holdout label: {label}')
            by_label[label] = item
        for r in holdouts:
            item = by_label.get(r['label'])
            if item is None:
                r['errors'].append('Holdout label is absent from frozen.json plan')
            else:
                expected_cap = 100 if r.get('arm') == 'native' else cap
                expected_policy = policy if r.get('arm') == 'ketris' else 'native'
                for field in ('arm', 'repeat', 'policy'):
                    if r.get(field) != item.get(field):
                        r['errors'].append(f'Holdout {field} differs from frozen.json plan')
                if item.get('cap') != expected_cap or r.get('caps') != [expected_cap]*7:
                    r['errors'].append('Holdout caps differ from frozen selected cap')
                if item.get('policy') != expected_policy or r.get('policy') != expected_policy:
                    r['errors'].append('Holdout policy differs from frozen selected policy')
                if selected.get('epoch_ns') is not None and r.get('start_epoch_ns', 0) < selected['epoch_ns']:
                    r['errors'].append('Holdout started before frozen.json was recorded')
            r['valid'] = not r['errors']
        return selected, errors
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, [f'Cannot audit frozen.json: {exc}']


def analyze(root, expected_repeats=5):
    root = Path(root).resolve()
    errors, warnings = [], []
    frozen = None
    manifest_path = next((p for p in (root/'frozen-manifest.json', root/'manifest.json') if p.is_file()), None)
    try:
        if manifest_path is None:
            raise ValueError('Missing frozen-manifest.json or manifest.json')
        manifest = read_json(manifest_path)
        frozen = canonical_workers(manifest['workers'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f'Frozen manifest: {exc}')
    directories = sorted(p for p in (root/'runs').glob('*') if p.is_dir())
    runs = [audit_run(p, frozen, root) for p in directories]
    if not runs:
        errors.append('No run directories found')
    selected, selection_errors = validate_frozen_selection(root, runs, frozen, expected_repeats)
    errors.extend(selection_errors)
    candidates = sorted((r for r in runs if r['valid']), key=lambda r:
                        (r.get('arm') != 'native', r.get('category') != 'holdout', r.get('start_ns', 0)))
    reference = candidates[0] if candidates else None
    provenance_reference = {}
    for field in ('source_sha256', 'model_config_sha256', 'libbless_sha256'):
        for candidate in candidates:
            value = next((w.get('identity', {}).get(field) for w in candidate['workers']
                          if w.get('identity', {}).get(field) is not None), None)
            if value is not None:
                provenance_reference[field] = dict(value=value, run=candidate['label'])
                break
    for r in runs:
        for worker in r['workers']:
            for field, entry in provenance_reference.items():
                value = worker.get('identity', {}).get(field)
                if field == 'libbless_sha256' and r.get('policy') == 'native':
                    continue
                if value != entry['value']:
                    r['errors'].append(f"{worker['id']}: identity {field} differs from immutable reference")
        r['valid'] = not r['errors']
    if reference:
        signatures = reference['token_signatures']
        input_signatures = reference['input_signatures']
        for r in runs:
            mismatches = sorted(k for k in signatures.keys() | r['token_signatures'].keys()
                                if signatures.get(k) != r['token_signatures'].get(k))
            r['output_equivalent_to_reference'] = not mismatches
            if mismatches:
                r['errors'].append('Output token mismatch versus reference for ' + ', '.join(mismatches))
                r['valid'] = False
            input_mismatches = sorted(k for k in input_signatures.keys() | r['input_signatures'].keys()
                                      if input_signatures.get(k) != r['input_signatures'].get(k))
            r['input_equivalent_to_reference'] = not input_mismatches
            if input_mismatches:
                r['errors'].append('Input SHA256 mismatch versus reference for ' + ', '.join(input_mismatches))
                r['valid'] = False
    else:
        errors.append('No structurally valid run available as output reference')
    groups = {}
    holdouts = [r for r in runs if r.get('category') == 'holdout']
    for arm in ARMS:
        arm_runs = [r for r in holdouts if r.get('arm') == arm]
        valid = [r for r in arm_runs if r['valid']]
        repeats = [r.get('repeat') for r in arm_runs]
        group_errors = []
        if len(arm_runs) != expected_repeats:
            group_errors.append(f'Expected {expected_repeats} holdouts, found {len(arm_runs)}')
        if len(set(repeats)) != len(repeats):
            group_errors.append('Duplicate repeat identifiers')
        if len(valid) != len(arm_runs):
            group_errors.append(f'{len(arm_runs)-len(valid)} invalid run(s), retained in runs and CSV')
        configurations = {(r.get('policy'), json.dumps(r.get('caps'))) for r in arm_runs}
        if len(configurations) > 1:
            group_errors.append('Holdout policy or caps changed within arm')
        groups[arm] = dict(run_count=len(arm_runs), valid_count=len(valid),
                           expected_repeats=expected_repeats, repeat_ids=repeats,
                           complete=not group_errors and len(valid)==expected_repeats,
                           errors=group_errors, statistics_scope='Valid holdouts only; incomplete groups cannot establish comparison')
        for metric in ('makespan_s', 'requests_per_s', 'output_tokens_per_s', 'workloadsets_per_s'):
            groups[arm][metric] = stats([r[metric] for r in valid])
        groups[arm]['device_busy_utilization_pct'] = stats([r['device_busy']['utilization_pct'] for r in valid
            if r.get('device_busy', {}).get('available')])
        groups[arm]['device_busy_coverage_pct'] = stats([r['device_busy']['coverage_pct'] for r in valid
            if r.get('device_busy', {}).get('available')])
    comparisons = {}
    for baseline in ('native', 'spatial'):
        base, ket = groups[baseline], groups['ketris']
        comparison_errors = []
        if baseline == 'spatial':
            base_caps = {json.dumps(r.get('caps')) for r in holdouts if r.get('arm') == baseline}
            ketris_caps = {json.dumps(r.get('caps')) for r in holdouts if r.get('arm') == 'ketris'}
            if base_caps != ketris_caps:
                comparison_errors.append('Spatial MPS caps do not match KETRIS caps')
        complete = base['complete'] and ket['complete'] and not errors and not comparison_errors
        b, k = base['makespan_s']['mean'], ket['makespan_s']['mean']
        comparisons[baseline+'_vs_ketris'] = dict(valid=complete, baseline=baseline, errors=comparison_errors,
            baseline_mean_makespan_s=b, ketris_mean_makespan_s=k,
            index=100*b/k if complete and k else None,
            time_reduction_pct=100*(1-k/b) if complete and b else None,
            speedup=b/k if complete and k else None,
            index_formula='100 * mean(baseline actual makespan) / mean(KETRIS actual makespan)',
            time_reduction_formula='100 * (1 - mean(KETRIS actual makespan) / mean(baseline actual makespan))')
    chronological = sorted((r for r in runs if 'start_epoch_ns' in r), key=lambda r:r['start_epoch_ns'])
    sample_order = [dict(label=r['label'], category=r.get('category'), arm=r.get('arm'),
                        repeat=r.get('repeat'), start_epoch_ns=r['start_epoch_ns'],
                        end_epoch_ns=r.get('end_epoch_ns')) for r in chronological]
    for before, after in zip(chronological, chronological[1:]):
        if before.get('end_epoch_ns', 0) > after['start_epoch_ns']:
            warnings.append(f"Trials overlap in wall time: {before['label']} and {after['label']}")
    return dict(schema_version=1, root=str(root), valid=not errors and all(g['complete'] for g in groups.values()) and all(c['valid'] for c in comparisons.values()),
                errors=errors, warnings=warnings, expected_repeats=expected_repeats,
                frozen_manifest=str(manifest_path) if manifest_path else None,
                frozen_workers=frozen, frozen_selection=selected,
                provenance_reference=provenance_reference,
                output_reference_run=reference['label'] if reference else None,
                groups=groups, comparisons=comparisons, runs=runs, sample_order=sample_order,
                pilot_run_count=len(runs)-len(holdouts),
                definitions=dict(makespan='max(request completion_ns across all seven workers) - go.start_ns',
                    requests_per_s='actual completed requests / actual makespan_s',
                    output_tokens_per_s='actual output tokens / actual makespan_s',
                    workloadsets_per_s='1 / actual makespan_s',
                    std='Sample standard deviation (ddof=1); zero for n=1',
                    pilots='Excluded from all primary aggregates; retained and audited',
                    failure_handling='Every run retained; any invalid or missing holdout blocks its arm comparisons',
                    device_busy='Secondary NVML activity gauge over covered intervals; not useful-work efficiency'))


def write_outputs(result, root, plot=True):
    root = Path(root)
    (root/'analysis.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    columns = ['label','category','arm','repeat','policy','valid','makespan_s',
               'actual_requests','actual_output_tokens','requests_per_s','output_tokens_per_s',
               'workloadsets_per_s','start_epoch_ns','end_epoch_ns','output_equivalent_to_reference',
               'device_busy_utilization_pct','device_busy_coverage_pct',
               'device_busy_forward_pct','device_busy_backward_pct','errors']
    with (root/'results.csv').open('w', newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        for run in result['runs']:
            row={k:run.get(k) for k in columns}
            busy=run.get('device_busy',{})
            row.update(device_busy_utilization_pct=busy.get('utilization_pct'),
                       device_busy_coverage_pct=busy.get('coverage_pct'),
                       device_busy_forward_pct=busy.get('forward_utilization_pct'),
                       device_busy_backward_pct=busy.get('backward_utilization_pct'),
                       errors='; '.join(run['errors']))
            writer.writerow(row)
    if plot:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError:
            return
        eligible = [arm for arm in ARMS if result['groups'][arm]['makespan_s']['n']]
        if not eligible:
            return
        fig, ax = plt.subplots(figsize=(8, 4.5))
        means = [result['groups'][arm]['makespan_s']['mean'] for arm in eligible]
        stds = [result['groups'][arm]['makespan_s']['std'] for arm in eligible]
        labels = [arm.upper() if arm == 'ketris' else ('Native MPS' if arm == 'native' else 'Matched spatial MPS') for arm in eligible]
        ax.bar(labels, means, yerr=stds, capsize=6, color=['#637b9c','#b18654','#38826a'][:len(eligible)])
        ax.set_ylabel('All seven workloads completed: makespan (seconds)')
        ax.set_title('Fixed work, holdouts only — mean ± sample SD')
        if not result['valid']:
            ax.text(.5,.98,'INCOMPLETE / INVALID AUDIT: see analysis.json',transform=ax.transAxes,ha='center',va='top',color='#a32424',fontsize=9)
        ax.set_ylim(bottom=0)
        ax.grid(axis='y',alpha=.2)
        ax.set_axisbelow(True)
        fig.tight_layout()
        fig.savefig(root/'comparison.png',dpi=180)
        plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--expected-repeats',type=int,default=5)
    parser.add_argument('--no-plot',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test:
        return 0 if self_test() else 1
    if args.expected_repeats < 1:
        parser.error('--expected-repeats must be positive')
    result=analyze(args.root,args.expected_repeats)
    write_outputs(result,args.root,plot=not args.no_plot)
    print(json.dumps(dict(valid=result['valid'], errors=result['errors'], groups=result['groups'],comparisons=result['comparisons']),indent=2))
    return 0 if result['valid'] else 1


def self_test():
    import tempfile
    import unittest

    class AnalyzerTests(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.root = Path(self.tmp.name)
            self.workers = [dict(id=f'W{i}', requests=1, input_tokens=10+i,
                                 output_tokens=2) for i in range(7)]
            self.write(self.root / 'frozen-manifest.json', {'workers': self.workers})
            self.write(self.root / 'frozen.json', {
                'selected_cap': 100, 'selected_policy': 'phase90', 'workers': self.workers,
                'holdouts': [dict(label=f'holdout-{arm}-{repeat}', arm=arm, repeat=repeat,
                                 cap=100, policy='phase90' if arm=='ketris' else 'native')
                             for repeat in range(1,6) for arm in ARMS]})

        def tearDown(self):
            self.tmp.cleanup()

        def write(self, path, obj):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(obj))

        def run_fixture(self, arm, repeat, duration, category='holdout'):
            label = f'{category}-{arm}-{repeat}'
            run = self.root / 'runs' / label
            offset = (repeat * 100 + {'native': 0, 'spatial': 20, 'ketris': 40}[arm])
            start = (1000 + offset) * 10**9
            epoch = (1_790_000_000 + offset) * 10**9
            self.write(run / 'config.json', dict(category=category, arm=arm,
                       repeat=repeat, workers=self.workers, caps=[100]*7,
                       policy='phase90' if arm == 'ketris' else 'native'))
            self.write(run / 'go.json', dict(start_ns=start, start_epoch_ns=epoch))
            for worker in self.workers:
                end = start + int(duration * 1e9)
                req = dict(request_id=0, start_ns=start+1,
                           first_token_ns=start+2, completion_ns=end,
                           input_tokens=worker['input_tokens'], output_tokens=2,
                           token_ids=[100, 101], input_sha256='a'*64)
                self.write(run / f"result-{worker['id']}.json", dict(
                    id=worker['id'], requests=[req], started_ns=start+1,
                    completion_ns=end, identity=dict(uuid='GPU-fixture',
                    reported_sms=108, mps_cap=100, pid=20+int(worker['id'][1:]),
                    source_sha256='b'*64, model_config_sha256='c'*64,
                    libbless_sha256='d'*64 if arm=='ketris' else None)))
            self.write(run / 'cleanup-complete.json', {
                'children': {**{f'W{i}': {'pid': 20+i, 'returncode': 0} for i in range(7)},
                             'nvml': {'pid': 99, 'returncode': -15}},
                'no_live_workers': True, 'completed_epoch_ns': epoch+int((duration+1)*1e9)})
            self.write(run / 'summary.json', {
                'label': label, 'makespan_s': duration, 'completed_requests': 7,
                'generated_tokens': 14, 'end_epoch_ns': epoch+int(duration*1e9)})
            return run

        def complete(self):
            for repeat in range(1, 6):
                for arm, seconds in [('native', 10), ('spatial', 8), ('ketris', 5)]:
                    self.run_fixture(arm, repeat, seconds)
            result = analyze(self.root)
            self.assertIn('groups', result, 'analysis must expose aggregate results')
            return result

        def test_actual_makespan_denominators_and_pilot_exclusion(self):
            self.run_fixture('native', 0, 200, 'pilot')
            result = self.complete()
            self.assertEqual(result['groups']['native']['makespan_s']['mean'], 10)
            self.assertEqual(result['groups']['ketris']['requests_per_s']['mean'], 1.4)
            self.assertEqual(result['comparisons']['native_vs_ketris']['index'], 200)
            self.assertEqual(result['comparisons']['native_vs_ketris']['time_reduction_pct'], 50)
            self.assertEqual(result['comparisons']['spatial_vs_ketris']['index'], 160)
            self.assertTrue(result['valid'])

        def test_environment_string_caps_are_validated_numerically(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/result-W0.json'
            data=json.loads(p.read_text()); data['identity']['mps_cap']='100'
            self.write(p,data)
            self.assertTrue(analyze(self.root)['valid'])

        def test_spatial_comparison_requires_matching_caps(self):
            self.complete()
            for repeat in range(1,6):
                run=self.root/f'runs/holdout-spatial-{repeat}'
                p=run/'config.json'; data=json.loads(p.read_text()); data['caps']=[50]*7
                self.write(p,data)
                for index in range(7):
                    p=run/f'result-W{index}.json'; data=json.loads(p.read_text())
                    data['identity']['mps_cap']=50; self.write(p,data)
            result=analyze(self.root)
            self.assertTrue(result['comparisons']['native_vs_ketris']['valid'])
            self.assertFalse(result['comparisons']['spatial_vs_ketris']['valid'])

        def test_error_marker_rejects_an_otherwise_complete_run(self):
            self.complete()
            self.write(self.root/'runs/holdout-ketris-1/error.json', {'error': 'feeder failed'})
            result=analyze(self.root)
            run=next(r for r in result['runs'] if r['label']=='holdout-ketris-1')
            self.assertFalse(run['valid'])
            self.assertTrue(any('error.json' in e for e in run['errors']))
            self.assertIsNone(result['comparisons']['native_vs_ketris']['index'])

        def test_cleanup_requires_explicit_no_live_workers_and_zero_worker_exits(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/cleanup-complete.json'
            original=json.loads(p.read_text())
            cases=[]
            bad=copy.deepcopy(original); bad.pop('no_live_workers'); cases.append(bad)
            bad=copy.deepcopy(original); bad['no_live_workers']=False; cases.append(bad)
            bad=copy.deepcopy(original); bad['children']['W4']['returncode']=1; cases.append(bad)
            bad=copy.deepcopy(original); bad['children'].pop('W6'); cases.append(bad)
            for cleanup in cases:
                with self.subTest(cleanup=cleanup):
                    self.write(p,cleanup)
                    result=analyze(self.root)
                    run=next(r for r in result['runs'] if r['label']=='holdout-ketris-1')
                    self.assertFalse(run['valid'])
            self.write(p,original)
            self.assertTrue(analyze(self.root)['valid'])

        def test_orchestrator_summary_is_required_and_matches_raw_work(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/summary.json'
            original=json.loads(p.read_text())
            p.unlink()
            self.assertFalse(analyze(self.root)['valid'])
            for key, value in [('makespan_s', 4), ('completed_requests', 6),
                               ('generated_tokens', 13), ('end_epoch_ns', 0)]:
                with self.subTest(key=key):
                    bad=dict(original);bad[key]=value;self.write(p,bad)
                    self.assertFalse(analyze(self.root)['valid'])
            self.write(p,original)
            self.assertTrue(analyze(self.root)['valid'])

        def test_headerless_nvidia_capture_is_supported(self):
            run=self.run_fixture('native',1,1)
            epoch=json.loads((run/'go.json').read_text())['start_epoch_ns']/1e9
            with (run/'nvml.csv').open('w') as f:
                for delta in (-.1,.1,.3,.5,.7,.9,1.1):
                    stamp=datetime.fromtimestamp(epoch+delta,tz=timezone.utc).strftime('%Y/%m/%d %H:%M:%S.%f')
                    f.write(f'{stamp}, 70, 30, 180, 50, 1410, 1215, Not Active, Not Active, Not Active, Not Active\n')
            result=analyze(self.root)
            busy=result['runs'][0]['device_busy']
            self.assertTrue(busy['available'])
            self.assertAlmostEqual(busy['coverage_pct'],100,places=4)
            self.assertAlmostEqual(busy['utilization_pct'],70,places=4)

        def test_input_hash_mismatch_rejects_same_length_prompt(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/result-W0.json'
            data=json.loads(p.read_text()); data['requests'][0]['input_sha256']='f'*64
            self.write(p,data)
            result=analyze(self.root)
            run=next(r for r in result['runs'] if r['label']=='holdout-ketris-1')
            self.assertFalse(run['valid'])
            self.assertTrue(any('Input' in e for e in run['errors']))

        def test_source_model_and_library_hashes_cannot_change(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/result-W0.json'
            original=json.loads(p.read_text())
            for key in ('source_sha256','model_config_sha256','libbless_sha256'):
                with self.subTest(key=key):
                    data=copy.deepcopy(original);data['identity'][key]='f'*64;self.write(p,data)
                    result=analyze(self.root)
                    run=next(r for r in result['runs'] if r['label']=='holdout-ketris-1')
                    self.assertFalse(run['valid'])
            self.write(p,original)
            self.assertTrue(analyze(self.root)['valid'])

        def test_holdout_selection_and_plan_are_required(self):
            self.complete()
            p=self.root/'frozen.json';original=json.loads(p.read_text())
            p.unlink()
            self.assertFalse(analyze(self.root)['valid'])
            for key,value in [('selected_cap',50),('selected_policy','phase75')]:
                with self.subTest(key=key):
                    data=copy.deepcopy(original);data[key]=value;self.write(p,data)
                    self.assertFalse(analyze(self.root)['valid'])
            data=copy.deepcopy(original);data['holdouts'][0]['repeat']=99;self.write(p,data)
            self.assertFalse(analyze(self.root)['valid'])
            self.write(p,original)
            self.assertTrue(analyze(self.root)['valid'])

        def test_missing_worker_remains_visible_and_blocks_comparison(self):
            self.complete()
            (self.root/'runs/holdout-ketris-3/result-W6.json').unlink()
            result = analyze(self.root)
            self.assertEqual(result['groups']['ketris']['run_count'], 5)
            self.assertEqual(result['groups']['ketris']['valid_count'], 4)
            self.assertIsNone(result['comparisons']['native_vs_ketris']['index'])
            self.assertFalse(result['valid'])
            self.assertEqual(len(result['runs']), 15)

        def test_changed_token_ids_fail_equivalence(self):
            self.complete()
            p = self.root/'runs/holdout-spatial-2/result-W3.json'
            data=json.loads(p.read_text()); data['requests'][0]['token_ids']=[100,999]
            self.write(p,data)
            result=analyze(self.root)
            run=next(r for r in result['runs'] if r['label']=='holdout-spatial-2')
            self.assertFalse(run['valid'])
            self.assertTrue(any('token' in e.lower() for e in run['errors']))

        def test_changed_manifest_and_wrong_work_fail(self):
            self.complete()
            p=self.root/'runs/holdout-native-1/config.json'
            data=json.loads(p.read_text()); data['workers'][0]['requests']=2
            self.write(p,data)
            result=analyze(self.root)
            run=next(r for r in result['runs'] if r['label']=='holdout-native-1')
            self.assertFalse(run['valid'])
            self.assertTrue(any('manifest' in e.lower() for e in run['errors']))

        def test_invalid_request_timestamps_fail(self):
            self.complete()
            p=self.root/'runs/holdout-ketris-1/result-W0.json'
            data=json.loads(p.read_text()); data['requests'][0]['first_token_ns']=data['completion_ns']+1
            self.write(p,data)
            result=analyze(self.root)
            run=next(r for r in result['runs'] if r['label']=='holdout-ketris-1')
            self.assertFalse(run['valid'])

        def test_device_busy_uses_covered_time_and_reports_sensitivity(self):
            run=self.run_fixture('native',1,10)
            epoch=json.loads((run/'go.json').read_text())['start_epoch_ns']/1e9
            with (run/'nvml.csv').open('w') as f:
                f.write('timestamp, utilization.gpu [%]\n')
                for t,u in [(epoch-0.1,0),(epoch+0.1,100),(epoch+0.3,0),(epoch+9.9,100),(epoch+10.1,0)]:
                    f.write(f'{t},{u}\n')
            result=analyze(self.root)
            self.assertIn('runs', result, 'analysis must retain every run')
            busy=result['runs'][0]['device_busy']
            self.assertAlmostEqual(busy['covered_seconds'],0.4,places=5)
            self.assertAlmostEqual(busy['coverage_pct'],4,places=4)
            self.assertAlmostEqual(busy['forward_utilization_pct'],75,places=3)
            self.assertAlmostEqual(busy['backward_utilization_pct'],25,places=3)
            self.assertAlmostEqual(busy['utilization_pct'],50,places=3)
            self.assertGreater(busy['max_gap_s'],9)

        def test_duplicate_repeat_cannot_replace_missing_trial(self):
            self.complete()
            source=self.root/'runs/holdout-ketris-5'
            import shutil
            shutil.copytree(source,self.root/'runs/duplicate')
            result=analyze(self.root)
            self.assertFalse(result['groups']['ketris']['complete'])
            self.assertIsNone(result['comparisons']['native_vs_ketris']['index'])

    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AnalyzerTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    raise SystemExit(main())
