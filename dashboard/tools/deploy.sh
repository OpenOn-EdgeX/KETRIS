#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$TASK_ROOT"
MODE=${1:---dry-run=server}
[[ "$MODE" == '--apply' || "$MODE" == '--dry-run=server' ]] || exit 2
# Namespace must exist for server dry-run of namespaced resources.
kubectl apply -f dashboard/deploy/namespace.yaml
ARGS=(); [[ "$MODE" == '--apply' ]] || ARGS=(--dry-run=server)
if [[ "$MODE" == '--apply' ]] && ! kubectl get secret ketris-observer-ingest -n ketris >/dev/null 2>&1; then
  python3 -c 'import secrets;print(secrets.token_hex(32))' | kubectl create secret generic ketris-observer-ingest -n ketris --from-file=token=/dev/stdin
fi
kubectl apply "${ARGS[@]}" -f dashboard/deploy/serviceaccount.yaml -f dashboard/deploy/rbac.yaml -f dashboard/deploy/config.yaml -f dashboard/deploy/pvc.yaml -f dashboard/deploy/deployment.yaml -f dashboard/deploy/observer.yaml -f dashboard/deploy/service.yaml -f dashboard/deploy/networkpolicy.yaml
if [[ "$MODE" == '--apply' ]]; then
  kubectl rollout status deploy/ketris-dashboard -n ketris --timeout=180s
  kubectl rollout status ds/ketris-observer -n ketris --timeout=180s
fi
