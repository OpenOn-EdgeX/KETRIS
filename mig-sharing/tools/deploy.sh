#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$TASK_ROOT"
MODE=${1:---dry-run=server}
if [[ "$MODE" != '--apply' && "$MODE" != '--dry-run=server' ]]; then exit 2; fi
ARGS=(); [[ "$MODE" == '--apply' ]] || ARGS=(--dry-run=server)
kubectl apply "${ARGS[@]}" -f mig-sharing/deploy/rbac.yaml -f mig-sharing/deploy/config.yaml -f mig-sharing/deploy/daemonset.yaml
ADMISSION_ARGS=(); [[ "$MODE" != '--apply' ]] || ADMISSION_ARGS=(--apply)
PYTHONPATH="$TASK_ROOT/mig-sharing" python3 mig-sharing/tools/configure_admission.py "${ADMISSION_ARGS[@]}"
# Existing workloads use their unchanged controller/feedback/advertiser images.
# Actual core/webhook image updates are scoped and backed up by verify_live.py.
