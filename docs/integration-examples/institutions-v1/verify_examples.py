"""Compile/round-trip DESIGN EXAMPLES; never connects to or controls a service."""
import argparse
import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import grpc_tools
from google.protobuf import text_format


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    checks = []
    with tempfile.TemporaryDirectory(prefix='ketris-proto-examples-') as generated:
        subprocess.run([
            sys.executable, '-m', 'grpc_tools.protoc', '-I' + str(root),
            '-I' + str(Path(grpc_tools.__file__).parent / '_proto'),
            '--python_out=' + generated, '--grpc_python_out=' + generated,
            str(root / 'institution_examples.proto')], check=True)
        sys.path.insert(0, generated)
        pb = importlib.import_module('institution_examples_pb2')
        importlib.import_module('institution_examples_pb2_grpc')
        services = pb.DESCRIPTOR.services_by_name
        assert set(services) == {'ProfileIngestService', 'PlanAdmissionService'}
        checks.append('two proposed RPC services compile; no catalog service')

        def validate(message):
            if not hasattr(message, 'meta'):
                assert message.state in (pb.RECEIVED, pb.PLAN_ONLY)
                return
            m = message.meta
            assert all((m.schema_version, m.request_id, m.trace_id, m.workload_id, m.provider))
            assert m.origin == pb.SYNTHETIC and m.execution_mode == pb.CONTRACT_ONLY
            assert m.HasField('created_at') and m.HasField('expires_at')
            assert m.expires_at.seconds > m.created_at.seconds
            if isinstance(message, pb.PushProfileRequest):
                assert message.profile_id and message.profile_version > 0
                assert message.model and message.type and message.input_shape
                assert all(n > 0 for n in message.input_shape)
                if (message.gpu_req.HasField('mem') and
                        message.gpu_req.mem_unit == pb.MEMORY_UNIT_UNSPECIFIED):
                    assert not message.normalized.HasField('estimated_peak_memory_bytes')
            else:
                assert message.plan_id and message.revision > 0
                assert message.profile_id and message.profile_version > 0
                a = message.assignment
                assert a.assignment_id and a.WhichOneof('target') == 'fixture_target'
                assert a.fixture_target.fixture_ref
                p = a.sharing_policy
                assert p.HasField('space_percent') and 1 <= p.space_percent <= 100
                assert p.HasField('time_weight') and 1 <= p.time_weight <= 1000
                assert p.policy_source
                assert message.external_snapshot.origin == pb.SYNTHETIC

        messages = {}
        for path in sorted(root.glob('*.textproto')):
            cls = (pb.PushProfileRequest if path.name.startswith('yonsei-') else
                   pb.SubmitPartitionPlanRequest if path.name.startswith('skku-') else
                   pb.ProfileReceipt if path.name == 'profile-receipt.textproto' else pb.PlanReceipt)
            message = text_format.Parse(path.read_text(), cls())
            validate(message)
            restored = cls.FromString(message.SerializeToString())
            assert restored == message
            messages[path.name] = message
            checks.append(path.name + ': parse, semantic guards, binary round-trip')
        assert len(messages) == 7
        slide = messages['yonsei-slide-profile.textproto']
        assert slide.gpu_req.cores == 80 and slide.gpu_req.mem == 4000
        assert slide.gpu_req.cores_unit == pb.COMPUTE_UNIT_UNSPECIFIED
        checks.append('slide values preserved with unknown units; no normalized demand')
        for label in ('a', 'b'):
            profile = messages[f'yonsei-llm-{label}.textproto']
            plan = messages[f'skku-plan-{label}.textproto']
            assert (profile.profile_id, profile.profile_version, profile.meta.workload_id) == (
                plan.profile_id, plan.profile_version, plan.meta.workload_id)
            assert profile.meta.trace_id == plan.meta.trace_id
            checks.append(label + ': profile/version/workload/trace linkage')
        a, b = (messages[f'skku-plan-{v}.textproto'].assignment for v in ('a', 'b'))
        assert a.fixture_target == b.fixture_target
        assert (a.sharing_policy.time_weight, b.sharing_policy.time_weight) == (3, 1)
        checks.append('A/B reference same fixture with weights 3:1')
        for mutation in ('live_target_in_contract_only', 'cap_101', 'missing_profile_version'):
            bad = pb.SubmitPartitionPlanRequest()
            bad.CopyFrom(messages['skku-plan-a.textproto'])
            if mutation == 'live_target_in_contract_only':
                bad.assignment.live_mig_target.node_name = 'invalid-test-target'
            elif mutation == 'cap_101':
                bad.assignment.sharing_policy.space_percent = 101
            else:
                bad.profile_version = 0
            try:
                validate(bad)
            except AssertionError:
                checks.append('negative guard: ' + mutation + ' rejected')
            else:
                raise AssertionError(mutation + ' was accepted')
    report = {'artifact_kind': 'DESIGN_EXAMPLE', 'passed': True, 'checks': checks,
              'rpc_network_calls': 0, 'services_deployed': 0, 'hardware_execution': False,
              'note': 'Fixed example timestamps are not fresh live approvals.'}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
