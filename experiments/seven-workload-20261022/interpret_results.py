#!/usr/bin/env python3
"""Ancillary, read-only interpretation of fixed seven-worker trial evidence.

Run with --stdout while copies are incomplete; the default writes only
interpretation.json. This never modifies experiment inputs or primary analyses.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.dont_write_bytecode = True


TIME_LINE = re.compile(r'^total=(\d+) kernels=(\d+) credit=(-?\d+)$')
REFILL_LINE = re.compile(r'^refill_last_sequence=(\d+) accepted=(\d+) rejected=(\d+) '
                         r'active_cap_hits=(\d+) granted_us=(\d+) trimmed_us=(\d+)$')
GATE_LINE = re.compile(r'^gate_count=(\d+) gate_total_us=(\d+) gate_max_us=(\d+)$')
SNAPSHOT_FIELDS = ('charged_us', 'kernels', 'credit_us', 'refill_sequence',
                   'refill_accepted', 'refill_rejected', 'cap_hits', 'granted_us',
                   'trimmed_us', 'gate_count', 'gate_total_us', 'gate_max_us')


def interpret_gate_log(raw, before, after, start_ns, completion_ns, events):
    """Match complete ordered time/refill/gate blocks, then subtract the baseline.

    A refill sequence is its sender's monotonic timestamp, not the gate_stats
    reader's timestamp. Without command processing timestamps, even one nominal
    snapshot interval is not a guaranteed upper bound on processing latency.
    """
    errors, snapshots, pending = [], [], None
    for line_number, line in enumerate(raw.splitlines(), 1):
        match = TIME_LINE.fullmatch(line)
        if match:
            if pending is not None:
                errors.append('Incomplete log block before line '+str(line_number))
            pending = dict(zip(SNAPSHOT_FIELDS[:3], map(int, match.groups())))
            pending['first_line'] = line_number
            continue
        match = REFILL_LINE.fullmatch(line)
        if match:
            if pending is None or 'refill_sequence' in pending:
                errors.append('Unpaired refill log line '+str(line_number))
            else:
                pending.update(zip(SNAPSHOT_FIELDS[3:9], map(int, match.groups())))
            continue
        match = GATE_LINE.fullmatch(line)
        if match:
            if pending is None or 'refill_sequence' not in pending:
                errors.append('Unpaired gate log line '+str(line_number))
            else:
                pending.update(zip(SNAPSHOT_FIELDS[9:], map(int, match.groups())))
                pending['last_line'] = line_number
                snapshots.append(pending)
            pending = None
    if pending is not None:
        errors.append('Incomplete terminal log block')
    baseline_indices = [i for i, item in enumerate(snapshots)
                        if all(item[key] == before[key] for key in SNAPSHOT_FIELDS)]
    result = dict(valid=False, errors=errors, parsed_snapshot_count=len(snapshots),
                  exact_gate_timestamps_available=False,
                  timestamp_interpretation='Refill sender timestamps anchor cumulative snapshots; '
                      'gate reads occur later with unrecorded latency. Anchors are not event times '
                      'or rigorous observed-by deadlines. Event upper bounds use worker completion.',
                  sampled_credit_interpretation='Maximum of periodic log snapshots, not a continuous peak; '
                      'bank is an upper limit, and measured execution starts with zero credit.')
    if not baseline_indices:
        errors.append('No complete raw snapshot matches runtime-before.json')
        return result
    baseline = baseline_indices[-1]
    snapshots = snapshots[baseline:]
    result['excluded_prebaseline_snapshots'] = baseline
    if any(snapshots[-1][key] != after[key] for key in SNAPSHOT_FIELDS):
        errors.append('Terminal raw snapshot differs from runtime-after.json')
    event_positions = {event['at_ns']: i for i, event in enumerate(events)}
    increments, previous = [], snapshots[0]
    for item in snapshots[1:]:
        sequence = item['refill_sequence']
        if sequence and sequence not in event_positions:
            errors.append('Snapshot refill sequence absent from feeder-ticks.json')
        if sequence and item['refill_accepted']-before['refill_accepted'] != event_positions.get(sequence, -1)+1:
            errors.append('Snapshot accepted refill count differs from sequence position')
        for key in ('gate_count', 'gate_total_us', 'refill_accepted', 'refill_sequence'):
            if item[key] < previous[key]:
                errors.append('Counter decreased after measured baseline: '+key)
        change = item['gate_count']-previous['gate_count']
        if change > 0:
            prior_anchor = max(start_ns, previous['refill_sequence'])
            increments.append(dict(count_increment=change,
                cumulative_count_delta=item['gate_count']-before['gate_count'],
                gate_total_us_delta=item['gate_total_us']-before['gate_total_us'],
                refill_sequence_ns=sequence,
                refill_anchor_since_release_s=(sequence-start_ns)/1e9 if sequence else None,
                prior_snapshot_refill_anchor_since_release_s=(prior_anchor-start_ns)/1e9,
                conservative_event_lower_bound_s=(prior_anchor-start_ns)/1e9,
                conservative_event_upper_bound_s=(completion_ns-start_ns)/1e9,
                exact_observation_since_release_s=None,
                processing_latency_upper_bound_s=None,
                raw_log_line=item['last_line']))
        previous = item
    delta = after['gate_count']-before['gate_count']
    observed = sum(item['count_increment'] for item in increments)
    if observed != delta:
        errors.append('Observed positive gate increments differ from measured runtime counter delta')
    sequences = sorted({item['refill_sequence'] for item in snapshots if item['refill_sequence']})
    gaps = [(b-a)/1e9 for a, b in zip(sequences, sequences[1:])]
    result.update(valid=not errors, measured_snapshot_count=len(snapshots),
        runtime_gate_count_delta=delta, observed_gate_count_delta=observed,
        runtime_gate_total_us_delta=after['gate_total_us']-before['gate_total_us'],
        sampled_credit_peak_us=max(item['credit_us'] for item in snapshots),
        sampled_credit_minimum_us=min(item['credit_us'] for item in snapshots),
        snapshot_refill_anchor_gap_s=dict(median=statistics.median(gaps) if gaps else None,
                                         maximum=max(gaps) if gaps else None),
        first_gate_count_increment=increments[0] if increments else None,
        last_gate_count_increment=increments[-1] if increments else None,
        gate_count_increments=increments)
    return result


def paired_comparison(runs, baseline, study_complete):
    errors, by_key = [], {}
    for run in runs:
        if run.get('valid') and run['arm'] in (baseline, 'ketris'):
            key = (run['arm'], run['repeat'])
            if key in by_key:
                errors.append('Duplicate arm/repeat: '+str(key))
            by_key[key] = run
    pairs = []
    for repeat in range(1, 6):
        if all((arm, repeat) in by_key for arm in (baseline, 'ketris')):
            base = by_key[(baseline, repeat)]['makespan_s']
            ket = by_key[('ketris', repeat)]['makespan_s']
            pairs.append(dict(repeat=repeat, baseline_s=base, ketris_s=ket,
                difference_s=ket-base, relative_difference_pct=100*(ket/base-1)))
    differences = [pair['difference_s'] for pair in pairs]
    n = len(differences)
    mean = statistics.mean(differences) if n else None
    sd = statistics.stdev(differences) if n > 1 else None
    complete = study_complete and n == 5 and not errors
    t_critical = 2.7764451051977987  # Two-sided 0.975 Student t quantile, df=4.
    half = t_critical*sd/math.sqrt(n) if complete else None
    return dict(baseline=baseline, complete=complete, errors=errors, pairs=pairs,
        n=n, difference_definition='KETRIS minus baseline seconds; positive means slower',
        mean_difference_s=mean, sample_sd_difference_s=sd,
        positive_difference_pairs=sum(value > 0 for value in differences),
        ci95_mean_difference_s=[mean-half, mean+half] if complete else None,
        t_critical=t_critical if complete else None, degrees_of_freedom=4 if complete else None,
        inference_scope='Exploratory paired t interval for five repeat blocks; assumes independent '
            'approximately normal differences. Order was rotated, not randomized; no multiplicity '
            'adjustment. Interval suppressed unless the entire study passes both audits.')


def pooled_busy(runs):
    elapsed = sum(run['makespan_s'] for run in runs)
    available = [run['device_busy'] for run in runs if run.get('device_busy', {}).get('available')]
    covered = sum(row['covered_seconds'] for row in available)
    busy_seconds = sum(row['covered_seconds']*row['utilization_pct']/100 for row in available)
    return dict(run_count=len(runs), usable_capture_count=len(available),
        total_makespan_s=elapsed, covered_seconds=covered,
        integrated_device_busy_seconds_covered=busy_seconds,
        temporal_coverage_pct=100*covered/elapsed if elapsed else None,
        duration_weighted_utilization_pct=100*busy_seconds/covered if covered else None,
        definition='Sum of each trial trapezoidal NVML activity integral / total covered seconds; '
            'not an efficiency or useful-throughput metric. Missing intervals are not treated as zero.')


def read_json(path):
    return json.loads(path.read_text())


def interpret_run(directory, primary, runtime):
    import audit_runtime
    errors = []
    result = dict(label=directory.name, valid=False, errors=errors,
                  category=primary.get('category'), arm=primary.get('arm'),
                  repeat=primary.get('repeat'), makespan_s=primary.get('makespan_s'))
    errors.extend(primary.get('errors', []))
    errors.extend(runtime.get('errors', ['Missing corresponding runtime audit']))
    try:
        config, go = (read_json(directory/name) for name in ('config.json', 'go.json'))
        outputs = {f'W{i}': read_json(directory/f'result-W{i}.json') for i in range(7)}
        start = go['start_ns']
        end = max(output['completion_ns'] for output in outputs.values())
        completions = {worker: (output['completion_ns']-start)/1e9 for worker, output in outputs.items()}
        short = [completions[f'W{i}'] for i in range(3)]
        latest_short = max(short)
        remaining_long = sum(completions[f'W{i}'] > latest_short for i in range(3, 7))
        overlap = audit_runtime.worker_lifetime_overlap(outputs)
        result.update(worker_completion_since_release_s=completions,
            w0_w2_completion_range_s=[min(short), latest_short],
            all_seven_lifetime_overlap_s=overlap['overlap_duration_ms']/1000,
            all_seven_lifetime_overlap_fraction=overlap['overlap_duration_ms']/1e3/((end-start)/1e9),
            longest_workload_completion_s=(end-start)/1e9,
            long_workers_unfinished_when_all_short_workers_complete=remaining_long,
            drain_after_all_w0_w2_complete_s=(end-start)/1e9-latest_short,
            drain_after_all_w0_w2_complete_fraction=1-latest_short/((end-start)/1e9),
            phase_lifetimes=audit_runtime.phase_overlap(outputs, start, end),
            device_busy=primary.get('device_busy', {}), workers={})
        if config['policy'] != 'native':
            before, after, events = (read_json(directory/name) for name in
                ('runtime-before.json', 'runtime-after.json', 'feeder-ticks.json'))
            for worker, output in outputs.items():
                credit = audit_runtime.audit_credit(before[worker], after[worker], events,
                    worker, config['policy'], config['credit_bank_us'])
                gate = interpret_gate_log((directory/f'feed-{worker}.log').read_text(),
                    before[worker], after[worker], start, output['completion_ns'], events)
                result['workers'][worker] = dict(
                    reduced_refill_ticks=credit['reduced_refill_ticks'],
                    reduced_grants_relative_to_100pct_us=credit['reduced_grants_relative_to_100pct_us'],
                    reduction_divided_by_bank=credit['reduced_grants_relative_to_100pct_us']/config['credit_bank_us'],
                    bank_limit_us=config['credit_bank_us'], initial_credit_us=before[worker]['credit_us'],
                    final_credit_us=after[worker]['credit_us'],
                    final_runtime_debt_us=max(0, -after[worker]['credit_us']),
                    worker_completion_credit_us=output['credit_after']['credit_us'],
                    worker_completion_debt_us=max(0, -output['credit_after']['credit_us']),
                    gate_log=gate)
                errors.extend(worker+': '+message for message in gate['errors']+credit['errors'])
            result['gate_count_delta'] = sum(worker['gate_log']['runtime_gate_count_delta']
                                             for worker in result['workers'].values())
            result['summed_gate_duration_us'] = sum(worker['gate_log']['runtime_gate_total_us_delta']
                                                    for worker in result['workers'].values())
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        errors.append('Cannot interpret raw run: '+repr(exc))
    result['valid'] = not errors
    return result


def interpretation(root):
    import analyze
    import audit_runtime
    root = Path(root).resolve()
    primary = analyze.analyze(root, expected_repeats=5)
    runtime = audit_runtime.audit_study(root)
    runtime_by_label = {run['label']: run for run in runtime['runs']}
    runs, incomplete = [], []
    for run in primary['runs']:
        directory = root/'runs'/run['label']
        if not (directory/'summary.json').exists() or not (directory/'cleanup-complete.json').exists():
            incomplete.append(run['label'])
            continue
        runs.append(interpret_run(directory, run, runtime_by_label.get(run['label'], {})))
    holdouts = [run for run in runs if run['category'] == 'holdout']
    valid_runs = [run for run in holdouts if run['valid']]
    complete = (primary['valid'] and runtime['valid'] and len(holdouts) == 15
                and len(valid_runs) == 15 and not incomplete and all(run['valid'] for run in runs))
    groups = {}
    for arm in ('native', 'spatial', 'ketris'):
        selected = [run for run in valid_runs if run['arm'] == arm]
        short = [value for run in selected for value in run['w0_w2_completion_range_s']]
        overlap = [run['all_seven_lifetime_overlap_s'] for run in selected]
        drain = [run['drain_after_all_w0_w2_complete_fraction'] for run in selected]
        groups[arm] = dict(valid_holdout_count=len(selected),
            w0_w2_completion_range_s=[min(short), max(short)] if short else None,
            all_seven_lifetime_overlap_range_s=[min(overlap), max(overlap)] if overlap else None,
            drain_after_all_short_complete_fraction_range=[min(drain), max(drain)] if drain else None,
            pooled_device_busy=pooled_busy(selected))
    return dict(schema_version=1, complete=complete,
        status='complete_audited_five_repeats_per_arm' if complete else 'partial_or_invalid_do_not_treat_as_final',
        completed_holdout_count=len(holdouts), valid_holdout_count=len(valid_runs),
        expected_holdout_count=15, incomplete_raw_runs=incomplete,
        primary_audit_valid=primary['valid'], primary_audit_errors=primary['errors'],
        primary_group_errors={arm: group['errors'] for arm, group in primary['groups'].items()},
        runtime_audit_valid=runtime['valid'], runtime_audit_errors=runtime['errors'],
        source_sha256={name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                       for name in ('interpret_results.py', 'analyze.py', 'audit_runtime.py')},
        primary_comparisons=primary['comparisons'], groups=groups,
        paired_comparisons={baseline: paired_comparison(holdouts, baseline, complete)
                            for baseline in ('native', 'spatial')},
        runs=runs,
        claim_boundaries=[
            'Warm fixed batch: common release through final synchronized request completion, including drain; loading/warmup excluded.',
            'Seven workers start together; W0-W2 drain early, so this is not seven persistent backlogs.',
            'Per-client host elapsed credit accounting is not exclusive GPU time, measured GPU phase time, or kernel preemption.',
            'Bank limit is not starting credit; total reduced grants cannot alone show that a bank was exhausted.',
            'Gate counts include zero-credit startup; phase100 pilot is a diagnostic and not a repeated holdout control.',
            'Raw counter snapshot refill anchors are not exact gate event timestamps or rigorous observed-by deadlines.',
            'Summed gate time across processes is not makespan delay; waits can overlap.',
            'NVML busy is sampled device activity, not useful-work throughput or efficiency.',
            'Five sequential rotated blocks on one hardware/model/workload support a narrow result; t intervals are exploratory.'])


def self_test():
    import unittest

    class InterpretationTests(unittest.TestCase):
        def fixture(self):
            before = dict(charged_us=100, kernels=10, credit_us=0,
                          refill_sequence=0, refill_accepted=0, refill_rejected=0,
                          cap_hits=0, granted_us=0, trimmed_us=0,
                          gate_count=5, gate_total_us=50, gate_max_us=20)
            snapshots = [dict(before, gate_count=3, gate_total_us=30), before,
                dict(before, charged_us=200, kernels=20, credit_us=1000,
                     refill_sequence=1200000000, refill_accepted=1,
                     granted_us=1100, gate_count=7, gate_total_us=70),
                dict(before, charged_us=300, kernels=30, credit_us=2000,
                     refill_sequence=1400000000, refill_accepted=2,
                     granted_us=2200, gate_count=8, gate_total_us=80)]
            raw = ''.join(
                'total={charged_us} kernels={kernels} credit={credit_us}\n'
                'refill_last_sequence={refill_sequence} accepted={refill_accepted} '
                'rejected={refill_rejected} active_cap_hits={cap_hits} '
                'granted_us={granted_us} trimmed_us={trimmed_us}\n'
                'gate_count={gate_count} gate_total_us={gate_total_us} '
                'gate_max_us={gate_max_us}\n'.format(**item) for item in snapshots)
            events = [dict(at_ns=1200000000), dict(at_ns=1400000000)]
            return raw, before, snapshots[-1], 1000000000, 2000000000, events

        def test_warmup_exclusion_delta_and_uncertain_gate_times(self):
            result = interpret_gate_log(*self.fixture())
            self.assertEqual(result.get('excluded_prebaseline_snapshots'), 1)
            self.assertTrue(result['valid'], result)
            self.assertEqual(result['observed_gate_count_delta'], 3)
            self.assertEqual(result['sampled_credit_peak_us'], 2000)
            self.assertEqual(result['first_gate_count_increment']['count_increment'], 2)
            last = result['last_gate_count_increment']
            self.assertAlmostEqual(last['refill_anchor_since_release_s'], 0.4)
            self.assertAlmostEqual(last['conservative_event_lower_bound_s'], 0.2)
            self.assertEqual(last['conservative_event_upper_bound_s'], 1)
            self.assertIsNone(last['exact_observation_since_release_s'])
            self.assertIsNone(last['processing_latency_upper_bound_s'])

        def test_terminal_counter_mismatch_is_rejected(self):
            args = list(self.fixture())
            args[2] = dict(args[2], gate_count=9)
            result = interpret_gate_log(*args)
            self.assertEqual(result.get('valid'), False)
            self.assertTrue(result['errors'])

        def test_five_paired_differences_and_incomplete_suppression(self):
            runs = [dict(arm=arm, repeat=i, makespan_s=seconds, valid=True)
                    for i in range(1, 6)
                    for arm, seconds in [('native', 10), ('ketris', 10+i)]]
            result = paired_comparison(runs, 'native', True)
            self.assertEqual(result.get('mean_difference_s'), 3)
            self.assertAlmostEqual(result['sample_sd_difference_s'], math.sqrt(2.5))
            self.assertAlmostEqual(result['ci95_mean_difference_s'][0],
                                   3-2.7764451051977987/math.sqrt(2))
            self.assertEqual(result['positive_difference_pairs'], 5)
            self.assertIsNone(paired_comparison(runs, 'native', False)['ci95_mean_difference_s'])

        def test_busy_uses_duration_weights_and_reports_missing_coverage(self):
            runs = [dict(makespan_s=10, device_busy=dict(available=True,
                        covered_seconds=10, utilization_pct=50)),
                    dict(makespan_s=30, device_busy=dict(available=True,
                        covered_seconds=30, utilization_pct=100))]
            result = pooled_busy(runs)
            self.assertEqual(result.get('duration_weighted_utilization_pct'), 87.5)
            self.assertEqual(result['temporal_coverage_pct'], 100)
            runs.append(dict(makespan_s=10, device_busy=dict(available=False)))
            self.assertEqual(pooled_busy(runs)['temporal_coverage_pct'], 80)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InterpretationTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--stdout', action='store_true', help='Print JSON without writing any artifact')
    args = parser.parse_args()
    if args.self_test:
        return 0 if self_test() else 1
    root = args.root.resolve()
    result = interpretation(root)
    encoded = json.dumps(result, indent=2, allow_nan=False)+'\n'
    if args.stdout:
        print(encoded, end='')
    else:
        output = root/'interpretation.json'
        temporary = output.with_suffix('.json.tmp')
        temporary.write_text(encoded)
        temporary.replace(output)
        print(json.dumps(dict(complete=result['complete'], status=result['status'],
            valid_holdouts=result['valid_holdout_count'],
            invalid_runs=[dict(label=run['label'], errors=run['errors'])
                          for run in result['runs'] if not run['valid']], output=str(output)), indent=2))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
